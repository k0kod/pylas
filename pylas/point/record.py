""" Contains the classes that manages Las PointRecords
Las PointRecords are represented using Numpy's structured arrays,
The PointRecord classes provide a few extra things to manage these arrays
in the context of Las point data
"""
import logging
from abc import ABC, abstractmethod
from typing import NoReturn

import numpy as np

from . import dims, packing
from .. import errors
from ..point import PointFormat

logger = logging.getLogger(__name__)


def raise_not_enough_bytes_error(
        expected_bytes_len, missing_bytes_len, point_data_buffer_len, points_dtype
) -> NoReturn:
    raise errors.PylasError(
        "The file does not contain enough bytes to store the expected number of points\n"
        "expected {} bytes, read {} bytes ({} bytes missing == {} points) and it cannot be corrected\n"
        "{} (bytes) / {} (point_size) = {} (points)".format(
            expected_bytes_len,
            point_data_buffer_len,
            missing_bytes_len,
            missing_bytes_len / points_dtype.itemsize,
            point_data_buffer_len,
            points_dtype.itemsize,
            point_data_buffer_len / points_dtype.itemsize,
        )
    )


class IPointRecord(ABC):
    """ Wraps the numpy structured array contained the points data
    """

    @property
    @abstractmethod
    def point_format(self) -> PointFormat: ...

    @property
    @abstractmethod
    def array(self) -> np.ndarray: ...

    @property
    @abstractmethod
    def point_size(self):
        """ Shall return the point size as that will be written in the header
        """
        return self.point_format.size

    @property
    @abstractmethod
    def actual_point_size(self):
        """ Shall return the actual size in bytes that ta points take in memory
        """
        return self.array.dtype.itemsize

    @abstractmethod
    def __getitem__(self, item):
        pass

    @abstractmethod
    def __setitem__(self, key, value):
        pass

    @abstractmethod
    def __len__(self):
        pass

    @abstractmethod
    def raw_bytes(self):
        pass

    @abstractmethod
    def write_to(self, out):
        pass

    @classmethod
    @abstractmethod
    def from_stream(cls, stream, point_format_id, count):
        pass

    @classmethod
    @abstractmethod
    def empty(cls, point_format_id):
        pass


class PointRecord(IPointRecord, ABC):
    def __init__(self, data, point_format: PointFormat):
        self._array = data
        self._point_format = point_format

    @property
    def point_format(self) -> PointFormat:
        return self._point_format

    @property
    def array(self) -> np.ndarray:
        return self._array

    @property
    def dimensions_names(self):
        return self.point_format.dimension_names

    @property
    def extra_dimensions_names(self):
        """ Returns the names of extra-dimensions contained in the PointRecord
        """
        return self.point_format.extra_dimension_names

    @property
    def actual_point_size(self):
        """ Returns the point size in bytes taken by each points of the record

        Returns
        -------
        int
            The point size in byte

        """
        return self.array.dtype.itemsize

    @classmethod
    def from_point_record(cls, other_point_record, new_point_format):
        """  Construct a new PackedPointRecord from an existing one with the ability to change
        to point format while doing so
        """
        array = np.zeros_like(other_point_record.array, dtype=new_point_format.dtype)
        new_record = cls(array, new_point_format)
        new_record.copy_fields_from(other_point_record)
        return new_record

    def copy_fields_from(self, other_record):
        """ Tries to copy the values of the current dimensions from other_record
        """
        for dim_name in self.dimensions_names:
            try:
                self[dim_name] = other_record[dim_name]
            except ValueError:
                pass

    def add_extra_dims(self, type_tuples):
        self.point_format.extra_dims.extend(type_tuples)
        old_array = self.array
        self._array = np.zeros_like(old_array, dtype=self.point_format.dtype)
        self.copy_fields_from(old_array)

    def memoryview(self):
        return memoryview(self.array)

    def raw_bytes(self):
        return self.array.tobytes()

    def __getitem__(self, item):
        return self.array[item]

    def __setitem__(self, key, value):
        self._append_zeros_if_too_small(value)
        self.array[key] = value

    def _append_zeros_if_too_small(self, value):
        """ Appends zeros to the points stored if the value we are trying to
        fit is bigger
        """
        size_diff = len(value) - len(self.array)
        if size_diff:
            self._array = np.append(
                self.array, np.zeros(size_diff, dtype=self.array.dtype)
            )

    def __getattr__(self, item):
        try:
            return self[item]
        except ValueError:
            raise AttributeError("{} is not a valid dimension".format(item)) from None

    def __len__(self):
        return self.array.shape[0]


class PackedPointRecord(PointRecord):
    """
    In the PackedPointRecord, fields that are a combinations of many sub-fields (fields stored on less than a byte)
    are still packed together and are only de-packed and re-packed when accessed.

    This uses of less memory than if the sub-fields were unpacked
    However some operations on sub-fields require extra steps:

    >>> #return number is a sub-field
    >>> from pylas import PointFormat
    >>> packed_point_record = PackedPointRecord.zeros(PointFormat(0), 10)
    >>> packed_point_record['return_number'][:] = 1
    >>> np.alltrue(packed_point_record == 1)
    False

    >>> packed_point_record = PackedPointRecord.zeros(PointFormat(0), 10)
    >>> rn = packed_point_record['return_number']
    >>> rn[:] = 1
    >>> packed_point_record['return_number'] = rn
    >>> np.alltrue(packed_point_record['return_number'] == 1)
    True
    """

    def __init__(self, data, point_format=None):
        if point_format is None:
            point_format = PointFormat(dims.np_dtype_to_point_format(data.dtype))
        super().__init__(data, point_format)
        self.sub_fields_dict = point_format.sub_fields

    @property
    def all_dimensions_names(self):
        """ Returns all the dimensions names, including the names of sub_fields
        and their corresponding packed fields
        """
        return frozenset(self.array.dtype.names + tuple(self.sub_fields_dict.keys()))

    @property
    def point_size(self):
        """ Returns the point size in bytes taken by each points of the record

        Returns
        -------
        int
            The point size in byte

        """
        return self.array.dtype.itemsize

    @classmethod
    def zeros(cls, point_format, point_count):
        """ Creates a new point record with all dimensions initialized to zero

        Parameters
        ----------
        point_format: PointFormat
            The point format id the point record should have
        point_count : int
            The number of point the point record should have

        Returns
        -------
        PackedPointRecord

        """
        data = np.zeros(point_count, point_format.dtype)
        return cls(data, point_format)

    @classmethod
    def empty(cls, point_format):
        """ Creates an empty point record.

        Parameters
        ----------
        point_format: pylas.PointFormat
            The point format id the point record should have

        Returns
        -------
        PackedPointRecord

        """
        return cls.zeros(point_format, point_count=0)

    @classmethod
    def from_stream(cls, stream, point_format, count):
        """ Construct the point record by reading the points from the stream
        """
        points_dtype = point_format.dtype
        point_data_buffer = bytearray(stream.read(count * points_dtype.itemsize))

        try:
            data = np.frombuffer(point_data_buffer, dtype=points_dtype, count=count)
        except ValueError:
            expected_bytes_len = count * points_dtype.itemsize
            if len(point_data_buffer) % points_dtype.itemsize != 0:
                missing_bytes_len = expected_bytes_len - len(point_data_buffer)
                raise_not_enough_bytes_error(
                    expected_bytes_len,
                    missing_bytes_len,
                    len(point_data_buffer),
                    points_dtype,
                )
            else:
                actual_count = len(point_data_buffer) // points_dtype.itemsize
                logger.critical(
                    "Expected {} points, there are {} ({} missing)".format(
                        count, actual_count, count - actual_count
                    )
                )
                data = np.frombuffer(
                    point_data_buffer, dtype=points_dtype, count=actual_count
                )

        return cls(data, point_format)

    @classmethod
    def from_buffer(cls, buffer, point_format, count, offset=0):
        points_dtype = point_format.dtype
        data = np.frombuffer(buffer, dtype=points_dtype, offset=offset, count=count)

        return cls(data, point_format)

    def write_to(self, out):
        """ Writes the points to the output stream"""
        out.write(self.raw_bytes())

    def to_unpacked(self):
        arr = np.zeros_like(self.array, self.point_format.unpacked_dtype)
        record = UnpackedPointRecord(arr, self.point_format)
        record.copy_fields_from(self)
        return record

    def __getitem__(self, item):
        """ Gives access to the underlying numpy array
        Unpack the dimension if item is the name a sub-field
        """
        try:
            composed_dim, sub_field = self.sub_fields_dict[item]
            return packing.unpack(
                self.array[composed_dim], sub_field.mask, dtype=sub_field.type
            )
        except KeyError:
            return self.array[item]

    def __setitem__(self, key, value):
        """ Sets elements in the array
        """
        self._append_zeros_if_too_small(value)
        try:
            composed_dim, sub_field = self.sub_fields_dict[key]
            try:
                packing.pack(self.array[composed_dim], value, sub_field.mask, inplace=True)
            except OverflowError as e:
                raise OverflowError("Overflow when packing {} into {}: {}".format(
                    sub_field.name, composed_dim, e
                ))
        except KeyError:
            self.array[key] = value

    def __repr__(self):
        return "<PackedPointRecord(fmt: {}, len: {}, point size: {})>".format(
            self.point_format, len(self), self.actual_point_size
        )


class ScaleAwarePointRecord(PackedPointRecord):

    def __init__(self, array, point_format, scales, offsets):
        super().__init__(array, point_format)
        self.scales = scales
        self.offsets = offsets

    def __getitem__(self, item):
        if isinstance(item, (slice, np.ndarray)):
            return ScaleAwarePointRecord(self.array[item], self.point_format, self.scales, self.offsets)

        if item == "x":
            return (self["X"] * self.scales[0]) + self.offsets[0]
        elif item == "y":
            return (self["Y"] * self.scales[1]) + self.offsets[1]
        elif item == "z":
            return (self["Z"] * self.scales[2]) + self.offsets[2]
        else:
            return super(ScaleAwarePointRecord, self).__getitem__(item)


class UnpackedPointRecord(PointRecord):
    """
    In the Unpacked Point Record, all the sub-fields are un-packed meaning that they are in their
    own array.
    Because the minimum size for the elements of an array is 8 bits, and sub-fields are only a few bits
    (less than 8) the resulting unpacked array uses more memory, especially if the point format has lots of sub-fields
    """

    def __init__(self, data, point_fmt_id=None):
        if point_fmt_id is None:
            point_fmt_id = dims.np_dtype_to_point_format(data.dtype, unpacked=True)
        super().__init__(data, point_fmt_id)

    @property
    def point_size(self):
        return self.point_format.dtype.itemsize

    def write_to(self, out):
        out.write(self.to_packed().raw_bytes())

    @classmethod
    def from_stream(cls, stream, point_format_id, count, extra_dims=None):
        return UnpackedPointRecord.from_stream(
            stream, point_format_id, count, extra_dims=extra_dims
        ).to_unpacked()

    @classmethod
    def from_compressed_buffer(
            cls, compressed_buffer, point_format_id, count, laszip_vlr
    ):
        return PackedPointRecord.from_compressed_buffer(
            compressed_buffer, point_format_id, count, laszip_vlr
        ).to_unpacked()

    @classmethod
    def empty(cls, point_format):
        data = np.zeros(0, dtype=point_format.dtype)
        return cls(data, point_format)

    def to_packed(self):
        return PackedPointRecord.from_point_record(self, self.point_format)


def scale_dimension(array_dim, scale, offset):
    return (array_dim * scale) + offset


def unscale_dimension(array_dim, scale, offset):
    return np.round((np.array(array_dim) - offset) / scale)
