import abc
import io
import logging
import subprocess
from copy import copy
from typing import BinaryIO, Optional

import lazrs
import numpy as np

from pylas import HeaderFactory, errors
from .compression import LazBackend
from .compression import find_laszip_executable
from .errors import PylasError
from .evlrs import EVLRList, RawEVLRList
from .point import dims
from .point.format import PointFormat
from .point.record import PointRecord
from .utils import ConveyorThread
from .vlrs.known import LasZipVlr
from .vlrs.vlrlist import VLRList, RawVLRList

logger = logging.getLogger(__name__)


class WriterBuilder:
    def __init__(self, header=None):
        self.header = header
        self.vlrs = None
        self.version = None
        self.point_format = None
        self.trust_header = False

    def with_header(self, header):
        self.header = copy(header)
        return self

    def with_vlrs(self, vlrs: VLRList):
        self.vlrs = vlrs
        return self

    def with_version(self, version: str):
        if self.header is not None:
            self.header = HeaderFactory.convert_header(self.header, version)
        else:
            self.header = HeaderFactory.new(version)


class LasWriter:
    def __init__(self, dest, header, vlrs=None, do_compress=False, laz_backends=tuple(LazBackend.detect_available()), closefd=True):
        self.closefd = closefd
        self.header = copy(header)
        self.header.partial_reset()
        self.header.maxs = [np.finfo("f8").min] * 3
        self.header.mins = [np.finfo("f8").max] * 3

        self.do_compress = do_compress
        self.laz_backends = laz_backends
        self.dest = dest

        if vlrs is None:
            self.vlrs = VLRList()
        else:
            self.vlrs = vlrs

        # These will be initialized on the first call to `write`
        self.point_format = None
        self.point_writer = None
        self.done = False

    def write(self, points: PointRecord):
        if not points:
            return

        if self.done:
            raise PylasError("Cannot write points anymore")

        if self.header.point_count == 0:
            dims.raise_if_version_not_compatible_with_fmt(points.point_format.id, self.header.version)
            self.point_format = points.point_format
            self.header.point_format_id = self.point_format.id
            self.header.point_size = self.point_format.size
            self.header.set_compressed(self.do_compress)

            # TODO extrabytes vlr
            if self.do_compress:
                self.point_writer = self._create_laz_backend(self.laz_backends)
            else:
                self.point_writer = UncompressedPointWriter(self.dest)

            self.point_writer.write_initial_header_and_vlrs(self.header, self.vlrs)
        elif points.point_format != self.point_format:
            raise PylasError("Incompatible point formats")

        self._update_header(points)
        self.point_writer.write_points(points)

    def write_evlrs(self, evlrs: EVLRList):
        if self.header.version < "1.4":
            raise PylasError("EVLRs are not supported on files with version less than 1.4")

        if len(evlrs) > 0:
            self.point_writer.done()
            self.done = True
            self.header.number_of_evlr = len(evlrs)
            self.header.start_of_first_evlr = self.dest.tell()
            # self.header.update_evlrs_info_in_stream(self.dest)
            raw_evlrs = RawEVLRList.from_list(evlrs)
            raw_evlrs.write_to(self.dest)

    def close(self):
        if self.point_writer is not None:
            if not self.done:
                self.point_writer.done()
            self.point_writer.write_updated_header(self.header)
        if self.closefd:
            self.dest.close()

    def _update_header(self, points: PointRecord):
        self.header.x_max = max(self.header.x_max, (points["X"].max() * self.header.x_scale) + self.header.x_offset)
        self.header.y_max = max(self.header.y_max, (points["Y"].max() * self.header.y_scale) + self.header.y_offset)
        self.header.z_max = max(self.header.z_max, (points["Z"].max() * self.header.z_scale) + self.header.z_offset)
        self.header.x_min = min(self.header.x_min, (points["X"].min() * self.header.x_scale) + self.header.x_offset)
        self.header.y_min = min(self.header.y_min, (points["Y"].min() * self.header.y_scale) + self.header.y_offset)
        self.header.z_min = min(self.header.z_min, (points["Z"].min() * self.header.z_scale) + self.header.z_offset)

        for i, count in zip(*np.unique(points.return_number, return_counts=True)):
            if i >= len(self.header.number_of_points_by_return):
                # np.unique sorts unique values
                break
            self.header.number_of_points_by_return[i - 1] += count
        self.header.point_count += len(points)

    def _create_laz_backend(self, laz_backends):
        if not laz_backends:
            raise PylasError("No LazBackend selected, cannot compress data")
        for backend in laz_backends:
            try:
                if backend == LazBackend.Laszip:
                    return LasZipProcessPointWriter(self.dest)
                elif backend == LazBackend.LazrsParallel:
                    return LazrsPointWriter(self.dest, self.point_format, parallel=True)
                elif backend == LazBackend.Lazrs:
                    return LazrsPointWriter(self.dest, self.point_format, parallel=False)
                else:
                    raise PylasError("Unknown LazBacked: {}".format(backend))
            except Exception as e:
                logger.error(e)
                last_error = e
        else:
            raise PylasError(f"No LazBackend could be initialized: {last_error}")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


class PointWriter(abc.ABC):

    @property
    @abc.abstractmethod
    def destination(self): ...

    def write_initial_header_and_vlrs(self, header, vlrs: VLRList):
        raw_vlrs = RawVLRList.from_list(vlrs)
        header.number_of_vlr = len(raw_vlrs)
        header.offset_to_point_data = header.size + raw_vlrs.total_size_in_bytes()

        header.write_to(self.destination)
        raw_vlrs.write_to(self.destination)

    @abc.abstractmethod
    def write_points(self, points): ...

    @abc.abstractmethod
    def done(self): ...

    def write_updated_header(self, header):
        self.destination.seek(0, io.SEEK_SET)
        header.write_to(self.destination)


class UncompressedPointWriter(PointWriter):

    def __init__(self, dest):
        self.dest = dest

    @property
    def destination(self):
        return self.dest

    def write_points(self, points):
        self.dest.write(points.memoryview())

    def done(self):
        pass


class LasZipProcessPointWriter(PointWriter):

    def __init__(self, dest):
        laszip_binary = find_laszip_executable()
        self.dest = dest

        self.conveyor: Optional[ConveyorThread]
        try:
            _ = dest.fileno()
        except OSError:
            self.dest = dest
            self.process = subprocess.Popen([laszip_binary, "-stdin", '-olaz', "-stdout"],
                                            stdin=subprocess.PIPE,
                                            stdout=subprocess.PIPE,
                                            stderr=subprocess.PIPE)

            self.conveyor = ConveyorThread(self.process.stdout, self.dest)
            self.conveyor.start()
        else:
            self.conveyor = None
            self.process = subprocess.Popen([laszip_binary, "-stdin", '-olaz', "-stdout"],
                                            stdin=subprocess.PIPE,
                                            stdout=self.dest,
                                            stderr=subprocess.PIPE)

    @property
    def destination(self):
        return self.process.stdin

    def write_initial_header_and_vlrs(self, header, vlrs):
        header.set_point_count_to_max()
        header.set_compressed(False)
        super(LasZipProcessPointWriter, self).write_initial_header_and_vlrs(header, vlrs)
        header.set_compressed(True)
        header.point_count = 0

    def write_points(self, points):
        if self.process.poll() is not None:
            self.raise_if_bad_err_code()
        else:
            try:
                self.process.stdin.write(points.memoryview())
            except BrokenPipeError:
                raise errors.LazError("Laszip process failed: {}".format(self.process.stderr.read().decode())) from None

    def done(self):
        self.process.stdin.flush()
        self.process.stdin.close()
        self.process.wait()
        if self.conveyor is not None:
            self.conveyor.join()

        self.raise_if_bad_err_code()
        self._rewrite_chunk_table_offset()

    def _rewrite_chunk_table_offset(self):
        position_backup = self.dest.tell()
        self.dest.seek(0, io.SEEK_SET)
        hdr = HeaderFactory.read_from_stream(self.dest)
        self.dest.seek(hdr.offset_to_point_data, io.SEEK_SET)
        offset_to_chunk_table = int.from_bytes(self.dest.read(8), 'little', signed=True)
        if offset_to_chunk_table == -1:
            self.dest.seek(-8, io.SEEK_END)
            offset_to_chunk_table = int.from_bytes(self.dest.read(8), 'little', signed=True)
            self.dest.seek(hdr.offset_to_point_data, io.SEEK_SET)
            self.dest.write(offset_to_chunk_table.to_bytes(8, 'little', signed=True))
            self.dest.seek(-8, io.SEEK_END)
            self.dest.truncate()
        else:
            self.dest.seek(position_backup, io.SEEK_SET)

    def write_updated_header(self, header):
        self.dest.seek(0, io.SEEK_SET)
        hdr = HeaderFactory.read_from_stream(self.dest)
        hdr.point_count = header.point_count
        hdr.maxs = header.maxs
        hdr.mins = header.mins
        hdr.number_of_points_by_return = header.number_of_points_by_return

        if header.version >= '1.4':
            hdr.number_of_evlr = header.number_of_evlr
            hdr.start_of_first_evlr = header.start_of_first_evlr

        self.dest.seek(0, io.SEEK_SET)
        hdr.write_to(self.dest)

    def raise_if_bad_err_code(self):
        if self.process.returncode != 0:
            error_msg = self.process.stderr.read().decode()
            raise errors.LazError(
                "Laszip failed to {} with error code {}\n\t{}".format("compress", self.process.returncode,
                                                                      "\n\t".join(error_msg.splitlines())))


class LazrsPointWriter(PointWriter):
    def __init__(self, dest: BinaryIO, point_format: PointFormat, parallel: bool):
        self.dest = dest
        self.vlr = lazrs.LazVlr.new_for_compression(point_format.id, point_format.num_extra_bytes)
        self.parallel = parallel
        self.compressor = None

    def write_initial_header_and_vlrs(self, header, vlrs: VLRList):
        laszip_vlr = LasZipVlr(self.vlr.record_data())
        vlrs.append(laszip_vlr)
        super().write_initial_header_and_vlrs(header, vlrs)
        # We have to initialize our compressor here
        # because on init, it writes the offset to chunk table
        # so the header and vlrs have to be written
        if self.parallel:
            self.compressor = lazrs.ParLasZipCompressor(self.dest, self.vlr)
        else:
            self.compressor = lazrs.LasZipCompressor(self.dest, self.vlr)

    @property
    def destination(self):
        return self.dest

    def write_points(self, points):
        points_bytes = np.frombuffer(points.array, np.uint8)
        self.compressor.compress_many(points_bytes)

    def done(self):
        if self.compressor is not None:
            self.compressor.done()
