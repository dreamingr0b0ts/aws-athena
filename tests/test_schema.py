"""Test the S3 prefix sizer with a fake boto3 S3 client."""

from athena_toolkit.client import AwsClients
from athena_toolkit.config import AthenaConfig
from athena_toolkit.schema import Catalog


class FakePaginator:
    def __init__(self, pages):
        self._pages = pages

    def paginate(self, **kwargs):
        yield from self._pages


class FakeS3:
    def __init__(self, pages):
        self._pages = pages
        self.calls = []

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return FakePaginator(self._pages)


def make_catalog(s3):
    config = AthenaConfig(database="db")
    clients = AwsClients(config, session=object())
    clients.client = lambda service: s3  # type: ignore[assignment]
    return Catalog(config, clients=clients)


def test_sum_s3_size_adds_object_sizes_and_skips_markers():
    pages = [
        {"Contents": [
            {"Key": "data/part-0001.parquet", "Size": 1000},
            {"Key": "data/part-0002.parquet", "Size": 2000},
            {"Key": "data/_SUCCESS", "Size": 0},
            {"Key": "data/.hidden", "Size": 999},
            {"Key": "data/_$folder$", "Size": 500},
        ]},
        {"Contents": [
            {"Key": "data/part-0003.parquet", "Size": 3000},
        ]},
    ]
    cat = make_catalog(FakeS3(pages))
    assert cat.sum_s3_size("s3://my-bucket/data/") == 6000


def test_sum_s3_size_empty_or_non_s3_returns_zero():
    cat = make_catalog(FakeS3([]))
    assert cat.sum_s3_size(None) == 0
    assert cat.sum_s3_size("") == 0
    assert cat.sum_s3_size("/local/path") == 0
