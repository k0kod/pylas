import io

import numpy as np

from pylas.header import rawheader
from . import pointdata, vlr, pointdimensions
from .compression import (is_point_format_compressed,
                          compressed_id_to_uncompressed,
                          uncompressed_id_to_compressed,
                          compress_buffer,
                          create_laz_vlr)


def scale_dimension(array_dim, scale, offset):
    return (array_dim * scale) + offset


class LasData:
    def __init__(self, data_stream):
        self.data_stream = data_stream
        self.header = rawheader.RawHeader.read_from(self.data_stream)
        self.vlrs = vlr.VLRList.read_from(data_stream, num_to_read=self.header.number_of_vlr)

        if is_point_format_compressed(self.header.point_data_format_id):
            laszip_vlr = self.vlrs.extract_laszip_vlr()
            if laszip_vlr is None:
                raise ValueError('Could not find Laszip VLR')
            self.header.point_data_format_id = compressed_id_to_uncompressed(
                self.header.point_data_format_id)

            # first 8 bytes after header + vlr + evlrs is the offset to the laz chunk table
            self.save_me = self.data_stream.seek(8, io.SEEK_CUR)
            self.np_point_data = pointdata.NumpyPointData.from_compressed_stream(
                self.data_stream,
                self.header.point_data_format_id,
                self.header.number_of_point_records,
                laszip_vlr
            )
        else:
            self.np_point_data = pointdata.NumpyPointData.from_stream(
                self.data_stream,
                self.header.point_data_format_id,
                self.header.number_of_point_records
            )

        self.X = self.np_point_data['X']
        self.Y = self.np_point_data['Y']
        self.Z = self.np_point_data['Z']
        self.intensity = self.np_point_data['intensity']
        self.scan_angle_rank = self.np_point_data['scan_angle_rank']
        self.user_data = self.np_point_data['user_data']
        self.point_source_id = self.np_point_data['point_source_id']

        self.x = scale_dimension(self.X, self.header.x_scale, self.header.x_offset)
        self.y = scale_dimension(self.Y, self.header.y_scale, self.header.y_offset)
        self.z = scale_dimension(self.Z, self.header.z_scale, self.header.z_offset)

        # These dimensions have to be repacked together when writing
        self.return_number = pointdimensions.bit_transform(
            self.np_point_data['bit_fields'],
            pointdimensions.RETURN_NUMBER_LOW_BIT,
            pointdimensions.RETURN_NUMBER_HIGH_BIT
        )

        self.number_of_returns = pointdimensions.bit_transform(
            self.np_point_data['bit_fields'],
            pointdimensions.NUMBER_OF_RETURNS_LOW_BIT,
            pointdimensions.NUMBER_OF_RETURNS_HIGH_BIT
        )

        self.scan_direction_flag = pointdimensions.bit_transform(
            self.np_point_data['bit_fields'],
            pointdimensions.SCAN_DIRECTION_FLAG_LOW_BIT,
            pointdimensions.SCAN_DIRECTION_FLAG_HIGH_BIT
        )

        self.edge_of_flight_line = pointdimensions.bit_transform(
            self.np_point_data['bit_fields'],
            pointdimensions.EDGE_OF_FLIGHT_LINE_LOW_BIT,
            pointdimensions.EDGE_OF_FLIGHT_LINE_HIGH_BIT
        )

        # Split raw classification
        self.classification = pointdimensions.bit_transform(
            self.np_point_data['raw_classification'],
            pointdimensions.CLASSIFICATION_LOW_BIT,
            pointdimensions.CLASSIFICATION_HIGH_BIT
        )

        self.synthetic = pointdimensions.bit_transform(
            self.np_point_data['raw_classification'],
            pointdimensions.SYNTHETIC_LOW_BIT,
            pointdimensions.SYNTHETIC_HIGH_BIT,
        ).astype('bool')

        self.key_point = pointdimensions.bit_transform(
            self.np_point_data['raw_classification'],
            pointdimensions.KEY_POINT_LOW_BIT,
            pointdimensions.KEY_POINT_HIGH_BIT
        ).astype('bool')

        self.withheld = pointdimensions.bit_transform(
            self.np_point_data['raw_classification'],
            pointdimensions.WITHHELD_LOW_BIT,
            pointdimensions.WITHHELD_HIGH_BIT
        ).astype('bool')

    @property
    def gps_time(self):
        return self.np_point_data['gps_time']

    @gps_time.setter
    def gps_time(self, value):
        self.np_point_data['gps_time'] = value

    @property
    def red(self):
        return self.np_point_data['red']

    @red.setter
    def red(self, value):
        self.np_point_data['red'] = value

    @property
    def green(self):
        return self.np_point_data['green']

    @green.setter
    def green(self, value):
        self.np_point_data['green'] = value

    @property
    def blue(self):
        return self.np_point_data['blue']

    @blue.setter
    def blue(self, value):
        self.np_point_data['blue'] = value

    def repack_bit_fields(self):
        self.np_point_data['bit_fields'] = pointdata.bitpack(
            (self.return_number, self.number_of_returns, self.scan_direction_flag, self.edge_of_flight_line),
            ((pointdimensions.RETURN_NUMBER_LOW_BIT, pointdimensions.RETURN_NUMBER_HIGH_BIT),
             (pointdimensions.NUMBER_OF_RETURNS_LOW_BIT, pointdimensions.NUMBER_OF_RETURNS_HIGH_BIT),
             (pointdimensions.SCAN_DIRECTION_FLAG_LOW_BIT, pointdimensions.SCAN_DIRECTION_FLAG_HIGH_BIT),
             (pointdimensions.EDGE_OF_FLIGHT_LINE_LOW_BIT, pointdimensions.EDGE_OF_FLIGHT_LINE_HIGH_BIT)
             ))

    def repack_classification(self):
        self.np_point_data['raw_classification'] = pointdata.bitpack(
            (self.classification, self.synthetic, self.key_point, self.withheld),
            ((pointdimensions.CLASSIFICATION_LOW_BIT, pointdimensions.CLASSIFICATION_HIGH_BIT),
             (pointdimensions.SYNTHETIC_LOW_BIT, pointdimensions.SYNTHETIC_HIGH_BIT),
             (pointdimensions.KEY_POINT_LOW_BIT, pointdimensions.KEY_POINT_HIGH_BIT),
             (pointdimensions.WITHHELD_LOW_BIT, pointdimensions.WITHHELD_HIGH_BIT)
             ))

    def write_to(self, out_stream, do_compress=False):
        # self.repack_bit_fields()
        self.repack_classification()
        if do_compress:
            lazvrl = create_laz_vlr(self.header.point_data_format_id)
            self.vlrs.append(vlr.LasZipVlr(lazvrl.data()))

            self.header.offset_to_point_data = self.header.header_size + self.vlrs.total_size_in_bytes()
            self.header.point_data_format_id = uncompressed_id_to_compressed(self.header.point_data_format_id)
            self.header.number_of_vlr = len(self.vlrs)

            compressed_points = compress_buffer(
                np.frombuffer(self.np_point_data.data, np.uint8),
                lazvrl.schema,
                self.header.offset_to_point_data,
            )

            print('len of vlrs', self.vlrs.total_size_in_bytes())
            print('offset', self.header.offset_to_point_data)
            print('num vlr', self.header.number_of_vlr)

            self.header.write_to(out_stream)
            self.vlrs.write_to(out_stream)
            assert out_stream.tell() == self.header.offset_to_point_data
            out_stream.write(compressed_points.tobytes())
        else:
            self.header.number_of_vlr = len(self.vlrs)
            self.header.offset_to_point_data = self.header.header_size + self.vlrs.total_size_in_bytes()

            self.header.write_to(out_stream)
            self.vlrs.write_to(out_stream)
            self.np_point_data.write_to(out_stream)

    @classmethod
    def open(cls, source):
        if isinstance(source, bytes):
            return cls.from_buffer(source)
        elif isinstance(source, str):
            return cls.from_file(source)
        else:
            return cls(source)

    @classmethod
    def from_file(cls, filename):
        with open(filename, mode='rb') as f:
            return cls(f)

    @classmethod
    def from_buffer(cls, buf):
        with io.BytesIO(buf) as stream:
            return cls(stream)
