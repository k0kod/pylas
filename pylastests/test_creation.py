import numpy as np
import pytest

import pylas
from pylas import PointFormat
from pylastests.test_common import write_then_read_again, simple_las, test1_4_las


@pytest.fixture()
def file1_4():
    return pylas.read(test1_4_las)


@pytest.fixture()
def file():
    return pylas.read(simple_las)


def test_xyz():
    las = pylas.create()
    shape = (150,)
    las.X = np.zeros(shape, dtype=np.int32)
    las.Y = np.ones(shape, dtype=np.int32)
    las.Z = np.zeros(shape, dtype=np.int32)
    las.Z[:] = -152

    las = write_then_read_again(las)

    assert np.alltrue(las.X == 0)
    assert np.alltrue(las.Y == 1)
    assert np.alltrue(las.Z == -152)


def test_wrong_version():
    for i in range(6, 8):
        with pytest.raises(pylas.errors.PylasError):
            _ = pylas.create(point_format=i, file_version="1.2")


def test_good_version_is_used():
    for i in range(6, 8):
        las = pylas.create(point_format=i)
        assert las.header.version.major == 1
        assert las.header.version.minor == 4


def test_create_fmt_0():
    new = pylas.create(point_format=0)

    with pytest.raises(ValueError):
        new.red = np.zeros(len(new.points), np.uint16)

    with pytest.raises(ValueError):
        new.red = np.zeros(len(new.points), np.uint16)

    with pytest.raises(ValueError):
        new.red = np.zeros(len(new.points), np.uint16)

    with pytest.raises(ValueError):
        new.gps_time = np.zeros(len(new.points), np.float64)


def test_create_fmt_1():
    new = pylas.create(point_format=1)

    with pytest.raises(ValueError):
        new.red = np.zeros(len(new.points), np.uint16)

    with pytest.raises(ValueError):
        new.red = np.zeros(len(new.points), np.uint16)

    with pytest.raises(ValueError):
        new.red = np.zeros(len(new.points), np.uint16)

    gps_time = np.random.uniform(0, 25641, len(new.points))
    new.gps_time = gps_time
    assert np.allclose(new.gps_time, gps_time)

    new = write_then_read_again(new)
    assert np.allclose(new.gps_time, gps_time)


def test_create_fmt_2(file):
    new = pylas.create(point_format=2)

    with pytest.raises(ValueError):
        new.gps_time = file.gps_time

    new.red = file.red
    new.green = file.green
    new.blue = file.blue

    assert np.allclose(new.red, file.red)
    assert np.allclose(new.green, file.green)
    assert np.allclose(new.blue, file.blue)

    new = write_then_read_again(new)
    assert np.allclose(new.red, file.red)
    assert np.allclose(new.green, file.green)
    assert np.allclose(new.blue, file.blue)


def test_create_fmt_3(file):
    new = pylas.create(point_format=3)

    new.red = file.red
    new.green = file.green
    new.blue = file.blue
    new.gps_time = file.gps_time

    assert np.allclose(new.red, file.red)
    assert np.allclose(new.green, file.green)
    assert np.allclose(new.blue, file.blue)
    assert np.allclose(new.gps_time, file.gps_time)

    new = write_then_read_again(new)
    assert np.allclose(new.red, file.red)
    assert np.allclose(new.green, file.green)
    assert np.allclose(new.blue, file.blue)
    assert np.allclose(new.gps_time, file.gps_time)


def test_create_fmt_6(file1_4):
    new = pylas.create(point_format=6)
    assert str(new.header.version) == "1.4"

    dim_names_fmt_6 = PointFormat(6).dtype().names

    for dim_name in dim_names_fmt_6:
        new[dim_name] = file1_4[dim_name]

    for dim_name in dim_names_fmt_6:
        assert np.allclose(new[dim_name], file1_4[dim_name]), "{} not equal".format(
            dim_name
        )

    new = write_then_read_again(new)
    for dim_name in dim_names_fmt_6:
        assert np.allclose(new[dim_name], file1_4[dim_name]), "{} not equal".format(
            dim_name
        )
