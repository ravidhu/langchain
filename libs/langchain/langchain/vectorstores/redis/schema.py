import os
from enum import Enum
from pathlib import Path
from typing import Annotated, Dict, List, Optional, Union
from uuid import uuid4

import yaml
from pydantic import BaseModel, Field, root_validator, validator

# ignore type error here as it's a redis-py type problem
from redis.commands.search.field import (  # type: ignore
    GeoField,
    NumericField,
    TagField,
    TextField,
    VectorField,
)
from typing_extensions import Literal


class RedisDistanceMetric(str, Enum):
    l2 = "L2"
    cosine = "COSINE"
    ip = "IP"


class RedisField(BaseModel):
    name: str = Field(...)
    sortable: Optional[bool] = False


class TextFieldSchema(RedisField):
    weight: float = 1
    no_stem: bool = False
    phonetic_matcher: Optional[str] = None
    withsuffixtrie: bool = False

    def as_field(self):
        return TextField(
            self.name,
            weight=self.weight,
            no_stem=self.no_stem,
            phonetic_matcher=self.phonetic_matcher,
            sortable=self.sortable,
        )


class TagFieldSchema(RedisField):
    separator: str = ","
    case_sensitive: bool = False

    def as_field(self):
        return TagField(
            self.name,
            separator=self.separator,
            case_sensitive=self.case_sensitive,
            sortable=self.sortable,
        )


class NumericFieldSchema(RedisField):
    def as_field(self):
        return NumericField(self.name, sortable=self.sortable)


class GeoFieldSchema(RedisField):
    def as_field(self):
        return GeoField(self.name, sortable=self.sortable)


class RedisVectorField(BaseModel):
    name: str = Field(...)
    dims: int = Field(...)
    algorithm: object = Field(...)
    datatype: str = Field(default="FLOAT32")
    distance_metric: RedisDistanceMetric = Field(default="COSINE")
    initial_cap: int = Field(default=20000)

    @validator("datatype", "distance_metric", pre=True)
    def uppercase_strings(cls, v):
        return v.upper()


class FlatVectorField(RedisVectorField):
    algorithm: Literal["FLAT"] = "FLAT"
    block_size: int = Field(default=1000)

    def as_field(self):
        return VectorField(
            self.name,
            self.algorithm,
            {
                "TYPE": self.datatype,
                "DIM": self.dims,
                "DISTANCE_METRIC": self.distance_metric,
                "INITIAL_CAP": self.initial_cap,
                "BLOCK_SIZE": self.block_size,
            },
        )


class HNSWVectorField(RedisVectorField):
    algorithm: Literal["HNSW"] = "HNSW"
    m: int = Field(default=16)
    ef_construction: int = Field(default=200)
    ef_runtime: int = Field(default=10)
    epsilon: float = Field(default=0.8)

    def as_field(self):
        return VectorField(
            self.name,
            self.algorithm,
            {
                "TYPE": self.datatype,
                "DIM": self.dims,
                "DISTANCE_METRIC": self.distance_metric,
                "INITIAL_CAP": self.initial_cap,
                "M": self.m,
                "EF_CONSTRUCTION": self.ef_construction,
                "EF_RUNTIME": self.ef_runtime,
                "EPSILON": self.epsilon,
            },
        )


class RedisModel(BaseModel):
    tag: Optional[List[TagFieldSchema]] = None
    text: List[TextFieldSchema] = [TextFieldSchema(name="content")]
    numeric: Optional[List[NumericFieldSchema]] = None
    geo: Optional[List[GeoFieldSchema]] = None
    vector: List[Union[FlatVectorField, HNSWVectorField]] = Field(
        default_factory=lambda: [FlatVectorField(name="content_vector", dims=1536)]
    )
    content_key: str = "content"
    content_vector_key: str = "content_vector"

    @property
    def content_vector(self) -> Union[FlatVectorField, HNSWVectorField]:
        for field in self.vector:
            if field.name == self.content_vector_key:
                return field
        raise ValueError("No content_vector field found")

    @property
    def is_empty(self) -> bool:
        return all(
            field is None
            for field in [self.tag, self.text, self.numeric, self.geo, self.vector]
        )

    def get_fields(self) -> List["RedisField"]:
        redis_fields: List["RedisField"] = []
        if self.is_empty:
            return redis_fields

        for field_name in self.__fields__.keys():
            if field_name not in ["content_key", "content_vector_key"]:
                field_group = getattr(self, field_name)
                if field_group is not None:
                    for field in field_group:
                        redis_fields.append(field.as_field())
        return redis_fields

    @property
    def keys(self) -> List[str]:
        keys: List[str] = []
        if self.is_empty:
            return keys

        for field_name in self.__fields__.keys():
            field_group = getattr(self, field_name)
            if field_group is not None:
                for field in field_group:
                    if not isinstance(field, str):
                        keys.append(field.name)
        return keys


def read_schema(index_schema: Optional[Union[Dict[str, str], str, os.PathLike]]):
    # check if its a dict and return RedisModel otherwise, check if it's a path and
    # read in the file assuming it's a yaml file and return a RedisModel
    if isinstance(index_schema, dict):
        return RedisModel(**index_schema)  # type: ignore
    elif isinstance(index_schema, (str, Path)):
        if Path(index_schema).is_file():
            with open(index_schema, "rb") as f:
                return RedisModel(**yaml.safe_load(f))
    else:
        raise TypeError(
            f"index_schema must be a dict, path to a yaml file, or a yaml string. "
            f"Got {type(index_schema)}"
        )