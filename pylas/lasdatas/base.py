import logging

import numpy as np

from pylas.vlrs.known import ExtraBytesStruct, ExtraBytesVlr
from .. import errors, extradims
from ..compression import LazBackend
from ..laswriter import LasWriter
from ..point import record, dims, PointFormat
from ..point.dims import ScaledArrayView
from ..vlrs import vlrlist

logger = logging.getLogger(__name__)


class LasBase(object):
    """LasBase is the base of all the different LasData classes.
    These classes are objects that the user will interact with to manipulate las data.

    It connects the point record, header, vlrs together.

    To access points dimensions using this class you have two possibilities

    .. code:: python

        las = pylas.read('some_file.las')
        las.classification
        # or
        las['classification']


    .. note::
        using las['dimension_name']  is not possible with the scaled values of x, y, z


    """

    def __init__(self, *, header, vlrs=None, points=None):
        if points is None:
            points = record.PackedPointRecord.empty(PointFormat(header.point_format_id))
        self.__dict__["points_data"] = points
        self.header = header
        self.vlrs = vlrs if vlrs is not None else vlrlist.VLRList()

    @property
    def x(self):
        """Returns the scaled x positions of the points as doubles"""
        return ScaledArrayView(self.X, self.header.x_scale, self.header.x_offset)

    @property
    def y(self):
        """Returns the scaled y positions of the points as doubles"""
        return ScaledArrayView(self.Y, self.header.y_scale, self.header.y_offset)

    @property
    def z(self):
        """Returns the scaled z positions of the points as doubles"""
        return ScaledArrayView(self.Z, self.header.z_scale, self.header.z_offset)

    @x.setter
    def x(self, value):
        if len(value) > len(self.points_data):
            self.points_data.resize(len(value))
        self.x[:] = value

    @y.setter
    def y(self, value):
        if len(value) > len(self.points_data):
            self.points_data.resize(len(value))
        self.y[:] = value

    @z.setter
    def z(self, value):
        if len(value) > len(self.points_data):
            self.points_data.resize(len(value))
        self.z[:] = value

    @property
    def point_format(self):
        return self.points_data.point_format

    @property
    def points(self):
        """returns the numpy array representing the points

        Returns
        -------
        the Numpy structured array of points

        """
        return self.points_data.array

    @points.setter
    def points(self, value):
        """Setter for the points property,
        Takes care of changing the point_format of the file
        (as long as the point format of the new points it compatible with the file version)

        Parameters
        ----------
        value: numpy.array of the new points

        """
        if value.dtype != self.points.dtype:
            raise errors.IncompatibleDataFormat(
                "Cannot set points with a different point format, convert first"
            )
        new_point_record = record.PackedPointRecord(
            value, self.points_data.point_format
        )
        dims.raise_if_version_not_compatible_with_fmt(
            new_point_record.point_format.id, self.header.version
        )
        self.points_data = new_point_record
        self.update_header()

    def __getattr__(self, item):
        """Automatically called by Python when the attribute
        named 'item' is no found. We use this function to forward the call the
        point record. This is the mechanism used to allow the users to access
        the points dimensions directly through a LasData.

        Parameters
        ----------
        item: str
            name of the attribute, should be a dimension name

        Returns
        -------
        The requested dimension if it exists

        """
        return self.points_data[item]

    def __setattr__(self, key, value):
        """This is called on every access to an attribute of the instance.
        Again we use this to forward the call the the points record

        But this time checking if the key is actually a dimension name
        so that an error is raised if the user tries to set a valid
        LAS dimension even if it is not present in the field.
        eg: user tries to set the red field of a file with point format 0:
        an error is raised

        """
        if key in dims.DIMENSIONS or key in self.points_data.all_dimensions_names:
            self.points_data[key] = value
        else:
            super().__setattr__(key, value)

    def __getitem__(self, item):
        return self.points_data[item]

    def __setitem__(self, key, value):
        self.points_data[key] = value

    def add_extra_dim(self, name, type, description=""):
        """Adds a new extra dimension to the point record

        Parameters
        ----------
        name: str
            the name of the dimension
        type: str
            type of the dimension (eg 'uint8')
        description: str, optional
            a small description of the dimension
        """
        name = name.replace(" ", "_")
        type_id = extradims.get_id_for_extra_dim_type(type)
        extra_byte = ExtraBytesStruct(
            data_type=type_id, name=name.encode(), description=description.encode()
        )

        try:
            extra_bytes_vlr = self.vlrs.get("ExtraBytesVlr")[0]
        except IndexError:
            extra_bytes_vlr = ExtraBytesVlr()
            self.vlrs.append(extra_bytes_vlr)
        finally:
            extra_bytes_vlr.extra_bytes_structs.append(extra_byte)
            self.points_data.add_extra_dims([(name, type)])

    def update_header(self):
        """Update the information stored in the header
        to be in sync with the actual data.

        This method is called automatically when you save a file using
        :meth:`pylas.lasdatas.base.LasBase.write`
        """
        self.header.point_format_id = self.points_data.point_format.id
        self.header.point_count = len(self.points_data)
        self.header.point_data_record_length = self.points_data.point_size

        if len(self.points_data) > 0:
            self.header.x_max = self.x.max()
            self.header.y_max = self.y.max()
            self.header.z_max = self.z.max()

            self.header.x_min = self.x.min()
            self.header.y_min = self.y.min()
            self.header.z_min = self.z.min()

            unique, counts = np.unique(self.return_number, return_counts=True)
            self.header.number_of_points_by_return = counts

    def write_to(
            self, out_stream, do_compress=False, laz_backend=LazBackend.detect_available()
    ):
        """writes the data to a stream

        Parameters
        ----------
        out_stream: file object
            the destination stream, implementing the write method
        do_compress: bool, optional, default False
            Flag to indicate if you want the date to be compressed
        """
        with LasWriter(
                out_stream,
                self.header,
                self.vlrs,
                do_compress=do_compress,
                closefd=False,
                laz_backend=laz_backend,
        ) as writer:
            writer.write(self.points_data)

    @staticmethod
    def _raise_if_not_expected_pos(stream, expected_pos):
        if not stream.tell() == expected_pos:
            raise RuntimeError(
                "Writing, expected to be at pos {} but stream is at pos {}".format(
                    expected_pos, stream.tell()
                )
            )

    def write_to_file(self, filename, do_compress=None):
        """Writes the las data into a file

        Parameters
        ----------
        filename : str
            The file where the data should be written.
        do_compress: bool, optional, default None
            if None the extension of the filename will be used
            to determine if the data should be compressed
            otherwise the do_compress flag indicate if the data should be compressed
        """
        is_ext_laz = filename.split(".")[-1].lower() == "laz"
        if is_ext_laz and do_compress is None:
            do_compress = True

        with open(filename, mode="wb+") as out:
            self.write_to(out, do_compress=do_compress)

    def write(
            self, destination, do_compress=None, laz_backend=LazBackend.detect_available()
    ):
        """Writes to a stream or file

        When destination is a string, it will be interpreted as the path were the file should be written to,
        also if do_compress is None, the compression will be guessed from the file extension:

        - .laz -> compressed
        - .las -> uncompressed

        .. note::

            This means that you could do something like:
                # Create .laz but not compressed

                las.write('out.laz', do_compress=False)

                # Create .las but compressed

                las.write('out.las', do_compress=True)

            While it should not confuse Las/Laz readers, it will confuse humans so avoid doing it


        Parameters
        ----------
        destination: str or file object
            filename or stream to write to
        do_compress: bool, optional
            Flags to indicate if you want to compress the data
        """
        if isinstance(destination, str):
            self.write_to_file(destination)
        else:
            if do_compress is None:
                do_compress = False
            self.write_to(
                destination, do_compress=do_compress, laz_backend=laz_backend
            )

    def __repr__(self):
        return "<LasData({}.{}, point fmt: {}, {} points, {} vlrs)>".format(
            self.header.version_major,
            self.header.version_minor,
            self.points_data.point_format,
            len(self.points_data),
            len(self.vlrs),
        )
