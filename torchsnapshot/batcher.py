#!/usr/bin/env python3
# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

import asyncio
import copy
import os
import uuid
from collections import defaultdict
from concurrent.futures import Executor
from typing import Dict, List, Optional, Tuple

from .io_preparer import TensorBufferStager, TensorIOPreparer

from .io_types import BufferConsumer, BufferStager, BufferType, ReadReq, WriteReq
from .knobs import get_slab_size_threshold_bytes
from .manifest import ChunkedTensorEntry, Entry, ShardedTensorEntry, TensorEntry
from .serialization import Serializer


class BatchedBufferStager(BufferStager):
    def __init__(
        self,
        byte_range_to_buffer_stager: Dict[Tuple[int, int], BufferStager],
    ) -> None:
        self.byte_range_to_buffer_stager = byte_range_to_buffer_stager

        byte_ranges = sorted(byte_range_to_buffer_stager.keys())
        end = byte_ranges[0][1]
        for byte_range in byte_ranges[1:]:
            if byte_range[0] != end:
                raise AssertionError("The byte ranges are not consecutive.")
            end = byte_range[1]

        self.slab_sz_bytes: int = end

    async def stage_buffer(self, executor: Optional[Executor] = None) -> BufferType:
        slab = bytearray(self.slab_sz_bytes)
        staging_task_to_byte_range = {}
        staging_tasks = set()

        for byte_range, buffer_stager in self.byte_range_to_buffer_stager.items():
            task = asyncio.create_task(buffer_stager.stage_buffer(executor=executor))
            staging_task_to_byte_range[task] = byte_range
            staging_tasks.add(task)

        while len(staging_tasks) != 0:
            done, _ = await asyncio.wait(
                staging_tasks, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                staging_tasks.remove(task)
                buf = task.result()
                byte_range = staging_task_to_byte_range[task]
                if len(buf) != byte_range[1] - byte_range[0]:
                    # Just to be defensive
                    raise AssertionError(
                        "The size of the buffer generated by the buffer stager "
                        "does not match with the byte range associated with the "
                        "buffer stager. "
                        f"Buffer size: {len(buf)}, byte range: {byte_range}."
                    )
                slab[byte_range[0] : byte_range[1]] = buf
        return memoryview(slab)

    def get_staging_cost_bytes(self) -> int:
        return (
            sum(
                stager.get_staging_cost_bytes()
                for stager in self.byte_range_to_buffer_stager.values()
            )
        ) + self.slab_sz_bytes

    class Builder:
        def __init__(self) -> None:
            self.byte_ranges: List[Tuple[int, int]] = []
            self.buffer_stagers: List[BufferStager] = []

        def add_buffer_stager(
            self,
            byte_range: Tuple[int, int],
            buffer_stager: BufferStager,
        ) -> None:
            self.buffer_stagers.append(buffer_stager)
            self.byte_ranges.append(byte_range)

        def build(self) -> "BatchedBufferStager":
            return BatchedBufferStager(
                byte_range_to_buffer_stager=dict(
                    zip(self.byte_ranges, self.buffer_stagers)
                ),
            )


def batch_write_requests(  # noqa
    entries: List[Entry],
    write_reqs: List[WriteReq],
    slab_size_threshold_bytes: Optional[int] = None,
) -> Tuple[List[Entry], List[WriteReq]]:
    """
    Batch small write requests into fewer large write requests.

    For example, assuming the slab_size_threshold_bytes is 50MB and we have the
    following write requests:

        logical_path: foo, location: dir/foo, size: 30MB
        logical_path: bar, location: dir/bar, size: 30MB
        logical_path: baz, location: dir/baz, size: 30MB
        logical_path: qux, location: dir/qux, size: 30MB

    Without batching, the manifest would be like:

        foo:
            ...
            location: "dir/foo"
            byte_range: null
        bar:
            ...
            location: "dir/bar"
            byte_range: null
        baz:
            ...
            location: "dir/baz"
            byte_range: null
        qux:
            ...
            location: "dir/qux"
            byte_range: null

    Without batching, the manifest would be like:

        foo:
            ...
            location: "dir/batch_file_0"
            byte_range: [0, 31457280]
        bar:
            ...
            location: "dir/batch_file_0"
            byte_range: [31457280, 62914560]
        baz:
            ...
            location: "dir/batch_file_1"
            byte_range: [0, 31457280]
        qux:
            ...
            location: "dir/batch_file_1"
            byte_range: [31457280, 62914560]

    Args:
        entries: The entries associated with the write requests to batch.
        write_reqs: The write requests to batch.
        slab_size_threshold_bytes: The rough size of the file/object for each
            batched write request.

    Returns:
        The batched write requests and updated entries.
    """
    slab_size_threshold_bytes = (
        slab_size_threshold_bytes or get_slab_size_threshold_bytes()
    )
    batched_write_reqs = []
    slab_locations = [os.path.join("batched", str(uuid.uuid4()))]
    slabs: List[BatchedBufferStager.Builder] = [BatchedBufferStager.Builder()]
    curr_slab_sz_bytes = 0
    relocation: Dict[str, Tuple[str, int, int]] = {}  # (new_location, lower, upper)

    # Group write requests into slabs
    # TODO: bin-packing that optimizes for slab count would be nice
    for wr in write_reqs:
        # We have to know the exact byte range within the slab beforehand. This
        # is currently only possible with tensors that can be serialized with
        # buffer protocol (which covers majority of the cases).
        if not isinstance(wr.buffer_stager, TensorBufferStager) or not is_batchable(
            wr.buffer_stager.entry
        ):
            batched_write_reqs.append(wr)
            continue

        tensor_sz_bytes = TensorIOPreparer.get_tensor_size_from_entry(
            entry=wr.buffer_stager.entry
        )
        # If the tensor size is already greater than the max slab size, no
        # batching is needed.
        if tensor_sz_bytes >= slab_size_threshold_bytes:
            batched_write_reqs.append(wr)
            continue

        byte_range = (curr_slab_sz_bytes, curr_slab_sz_bytes + tensor_sz_bytes)
        curr_slab_sz_bytes += tensor_sz_bytes

        # Add the buffer stager to the current slab
        slabs[-1].add_buffer_stager(
            byte_range=byte_range,
            buffer_stager=wr.buffer_stager,
        )
        # Track the byte range within the slab for this write request. Later
        # we'll need this information to update the corresponding entry.
        relocation[wr.path] = (
            slab_locations[-1],
            *byte_range,
        )

        # Create a new slab if the current slab exceeds the limit
        if curr_slab_sz_bytes >= slab_size_threshold_bytes:
            slabs.append(BatchedBufferStager.Builder())
            slab_locations.append(os.path.join("batched", str(uuid.uuid4())))
            curr_slab_sz_bytes = 0

    # Convert each slab to a batched write request
    for slab_location, slab in zip(slab_locations, slabs):
        if len(slab.buffer_stagers) == 0:
            continue
        batched_write_reqs.append(
            WriteReq(
                path=slab_location,
                buffer_stager=slab.build(),
            ),
        )

    # Since we only update tensor write requests, we only need to update
    # TensorEntrys. TensorEntrys can be nested in ChunkedTensorEntry and
    # ShardedTensorEntry.
    entries = copy.deepcopy(entries)
    location_to_entry: Dict[str, TensorEntry] = {}
    for entry in entries:
        if isinstance(entry, TensorEntry):
            location_to_entry[entry.location] = entry
        elif isinstance(entry, ChunkedTensorEntry):
            for chunk in entry.chunks:
                location_to_entry[chunk.tensor.location] = chunk.tensor
        elif isinstance(entry, ShardedTensorEntry):
            for shard in entry.shards:
                location_to_entry[shard.tensor.location] = shard.tensor

    # Update the location and byte range in the entries
    for location, (new_location, lower, upper) in relocation.items():
        if location not in location_to_entry:
            raise RuntimeError(
                f"The tensor entry with the location {location} is not passed to batch_write."
            )
        location_to_entry[location].location = new_location
        location_to_entry[location].byte_range = [lower, upper]

    return entries, batched_write_reqs


class BatchedBufferConsumer(BufferConsumer):
    def __init__(
        self,
        byte_range_to_buffer_consumer: Dict[Tuple[int, int], BufferConsumer],
        buf_sz_bytes: int,
    ) -> None:
        self.byte_range_to_buffer_consumer = byte_range_to_buffer_consumer
        self.buf_sz_bytes = buf_sz_bytes

    async def consume_buffer(
        self, buf: bytes, executor: Optional[Executor] = None
    ) -> None:
        consume_tasks = [
            asyncio.create_task(
                buffer_consumer.consume_buffer(
                    buf[byte_range[0] : byte_range[1]], executor=executor
                )
            )
            for byte_range, buffer_consumer in self.byte_range_to_buffer_consumer.items()
        ]
        await asyncio.wait(consume_tasks)

    def get_consuming_cost_bytes(self) -> int:
        return self.buf_sz_bytes + sum(
            consumer.get_consuming_cost_bytes()
            for consumer in self.byte_range_to_buffer_consumer.values()
        )


def batch_read_requests(read_reqs: List[ReadReq]) -> List[ReadReq]:
    """
    Batch read requests pointing to the same file.

    For example, if we have a manifest like:

        foo:
            ...
            location: "dir/batch_file_0"
            byte_range: [0, 31457280]
        bar:
            ...
            location: "dir/batch_file_0"
            byte_range: [31457280, 62914560]
        baz:
            ...
            location: "dir/batch_file_1"
            byte_range: [0, 31457280]
        qux:
            ...
            location: "dir/batch_file_1"
            byte_range: [31457280, 62914560]

    Without batching, the read requests would be like:

        location: "dir/batch_file_0", byte_range: [0, 31457280], fulfills: foo
        location: "dir/batch_file_0", byte_range: [31457280, 62914560], fulfills: bar
        location: "dir/batch_file_1", byte_range: [0, 31457280], fulfills: baz
        location: "dir/batch_file_1", byte_range: [31457280, 62914560], fulfills: qux

    With batching, the read requests would be like:

        location: "dir/batch_file_0", byte_range: [0, 62914560], fulfills: foo, bar
        location: "dir/batch_file_1", byte_range: [0, 62914560], fulfills: baz, qux

    Args:
        read_reqs: The write requests to batch.

    Returns:
        The batched read requests.
    """
    batched_read_reqs = []

    location_to_ranged_read_reqs: Dict[str, List[ReadReq]] = defaultdict(list)
    location_to_byte_range: Dict[str, Tuple[int, int]] = {}
    for rr in read_reqs:
        byte_range = rr.byte_range
        # If the read request requires the whole file/object,
        # no batching is needed
        if byte_range is None:
            batched_read_reqs.append(rr)
            continue
        # Merge all byte ranges within each location into a single consecutive range
        # TODO: come up with a heuristic to avoid batching when write
        # amplification is severe.
        # TODO: if there are large hole in the batched read request, we should
        # split the batched read request into multiple batched read requests
        # based on some heuristic
        location_to_ranged_read_reqs[rr.path].append(rr)
        if rr.path not in location_to_byte_range:
            location_to_byte_range[rr.path] = byte_range
        location_to_byte_range[rr.path] = (
            min(location_to_byte_range[rr.path][0], byte_range[0]),
            max(location_to_byte_range[rr.path][1], byte_range[1]),
        )

    # Merge read requests that shares the same location into a single read request
    for location, rrs in location_to_ranged_read_reqs.items():
        byte_range_to_buffer_consumer = {}
        lower_bound = location_to_byte_range[location][0]
        for rr in rrs:
            byte_range = rr.byte_range
            if byte_range is None:
                raise AssertionError("It's impossible for byte_range to be None.")
            # Convert the byte range within the file/object to the byte range
            # within the buffer consumed by BatchedBufferConsumer.
            adjusted_byte_range = (
                byte_range[0] - lower_bound,
                byte_range[1] - lower_bound,
            )
            byte_range_to_buffer_consumer[adjusted_byte_range] = rr.buffer_consumer
        batched_read_req = ReadReq(
            path=location,
            buffer_consumer=BatchedBufferConsumer(
                byte_range_to_buffer_consumer=byte_range_to_buffer_consumer,
                buf_sz_bytes=byte_range[1] - byte_range[0],
            ),
            byte_range=location_to_byte_range[location],
        )
        batched_read_reqs.append(batched_read_req)
    return batched_read_reqs


def is_batchable(entry: Entry) -> bool:
    return (
        isinstance(entry, TensorEntry)
        and entry.serializer == Serializer.BUFFER_PROTOCOL.value
    )
