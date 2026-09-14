"""M2 model reading, downgrading and writing."""

from .convert import ConvertedAsset, convert_m2, inspect_m2
from .model import M2Model, parse_m2
from .skin import convert_skin, inspect_skin, parse_skin
from .anim import convert_anim, inspect_anim
from .write import write_md20

__all__ = [
    "M2Model", "parse_m2", "write_md20",
    "convert_m2", "inspect_m2", "ConvertedAsset",
    "convert_skin", "inspect_skin", "parse_skin",
    "convert_anim", "inspect_anim",
]
