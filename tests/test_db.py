"""Client databases: DBD parsing, DB2 decoding, DBC writing and the mapping."""

import json
import struct

import pytest

import db_fixtures as DF
from wotlkconv.db.convert import convert_db2, find_template, table_name_for
from wotlkconv.db.db2 import inspect_db2, parse_db2
from wotlkconv.db.dbc import DbcBuilder, DbcTable, inspect_dbc
from wotlkconv.db.dbd import DbdIndex, parse_definition
from wotlkconv.db.mapping import (MappingLibrary, TableMapping,
                                  TransformContext, apply_row, missing_sources)
from wotlkconv.errors import (ConversionError, MalformedFileError,
                              MissingDependencyError, UnsupportedFormatError)
from wotlkconv.listfile import Listfile
from wotlkconv.options import Options
from wotlkconv.report import Status


@pytest.fixture
def defs_dir(tmp_path):
    d = tmp_path / "definitions"
    d.mkdir()
    return d


def index_for(defs_dir, table, columns, layout="1FE1BDA4"):
    (defs_dir / f"{table}.dbd").write_text(DF.build_dbd(table, columns, layout))
    return DbdIndex(defs_dir)


# ---------------------------------------------------------------------------
# DBD
# ---------------------------------------------------------------------------
def test_definition_parses_columns_layouts_and_annotations():
    text = """COLUMNS
int ID
int ModelID
float Scale
string Name

LAYOUT 1FE1BDA4, 3B0C8F12
BUILD 9.0.1.34490
BUILD 9.0.2.36665-9.0.5.37503
COMMENT ignored
$id$ID<32>
ModelID<u16>
Scale
Name
"""
    definition = parse_definition(text, "Test")
    layout = definition.by_hash("1FE1BDA4")
    assert layout is not None
    assert definition.by_hash(0x3B0C8F12) is layout
    names = [c.name for c in layout.columns]
    assert names == ["ID", "ModelID", "Scale", "Name"]
    assert layout.columns[0].is_id
    assert layout.columns[1].bit_width == 16 and not layout.columns[1].signed
    assert layout.columns[2].type == "float"
    assert layout.columns[3].type == "string"


def test_definition_matches_a_build_range():
    text = ("COLUMNS\nint ID\n\nLAYOUT ABCD1234\n"
            "BUILD 9.0.2.36665-9.0.5.37503\n$id$ID<32>\n")
    definition = parse_definition(text, "Test")
    assert definition.by_build("9.0.3.37000") is not None
    assert definition.by_build("8.3.0.34220") is None


def test_array_and_noninline_annotations():
    text = ("COLUMNS\nint ID\nfloat GeoBox\n\nLAYOUT A1\n"
            "$noninline,id$ID<32>\nGeoBox[6]\n")
    layout = parse_definition(text, "T").by_hash("A1")
    assert layout.columns[0].non_inline and layout.columns[0].is_id
    assert layout.columns[1].array_size == 6
    assert [c.name for c in layout.inline_columns()] == ["GeoBox"]


def test_missing_definitions_directory_is_falsy():
    assert not DbdIndex(None)
    assert DbdIndex(None).get("Anything") is None


def test_explicit_but_empty_dbd_directory_is_an_error(tmp_path):
    with pytest.raises(MissingDependencyError, match="WoWDBDefs"):
        DbdIndex.discover(tmp_path / "nope")


# ---------------------------------------------------------------------------
# DB2 decoding
# ---------------------------------------------------------------------------
BASIC_COLUMNS = [
    DF.Col("ID", "int", 32, is_id=True),
    DF.Col("FileDataID", "int", 32),
    DF.Col("ModelScale", "float", 32),
    DF.Col("ModelName", "string", 32),
    DF.Col("GeoBox", "float", 32, array=6),
    DF.Col("Flags", "int", 8, signed=False),
]
BASIC_ROWS = [
    {"ID": 100, "FileDataID": 900001, "ModelScale": 1.5, "ModelName": "bear",
     "GeoBox": [-1.0, -2.0, 0.0, 1.0, 2.0, 3.0], "Flags": 7},
    {"ID": 200, "FileDataID": 900002, "ModelScale": 0.75, "ModelName": "wolf",
     "GeoBox": [-0.5, -0.5, 0.0, 0.5, 0.5, 1.0], "Flags": 3},
]


def test_plain_columns_strings_floats_and_arrays(defs_dir):
    index = index_for(defs_dir, "Basic", BASIC_COLUMNS)
    table = parse_db2(DF.build_wdc3(BASIC_COLUMNS, BASIC_ROWS), "Basic.db2",
                      index, "Basic")
    assert table.named and sorted(table.rows) == [100, 200]
    assert table.rows[100]["ModelName"] == "bear"
    assert table.rows[100]["ModelScale"] == pytest.approx(1.5)
    assert table.rows[100]["GeoBox"] == [-1.0, -2.0, 0.0, 1.0, 2.0, 3.0]
    assert table.rows[200]["Flags"] == 3          # sub-byte width
    assert table.rows[200]["FileDataID"] == 900002


@pytest.mark.parametrize("magic", ["WDC1", "WDC2", "1SLC", "WDC3", "WDC4",
                                   "WDC5"])
def test_every_wdc_magic_decodes(defs_dir, magic):
    index = index_for(defs_dir, "Basic", BASIC_COLUMNS)
    table = parse_db2(DF.build_wdc3(BASIC_COLUMNS, BASIC_ROWS, magic=magic),
                      "Basic.db2", index, "Basic")
    assert table.magic == magic
    assert table.rows[200]["ModelName"] == "wolf"


def test_every_field_storage_type(defs_dir):
    columns = [
        DF.Col("ID", "int", 32, is_id=True),
        DF.Col("Packed", "int", 12, storage=DF.STORAGE_BITPACKED, signed=False),
        DF.Col("Signed", "int", 10, storage=DF.STORAGE_BITPACKED_SIGNED),
        DF.Col("Common", "int", 32, storage=DF.STORAGE_COMMON_DATA,
               common={7: 42}, common_default=99),
        DF.Col("Pallet", "int", 4, storage=DF.STORAGE_BITPACKED_INDEXED,
               signed=False),
        DF.Col("PalArray", "float", 4, array=3,
               storage=DF.STORAGE_BITPACKED_INDEXED_ARRAY),
    ]
    rows = [
        {"ID": 7, "Packed": 4095, "Signed": -300, "Pallet": 1234567,
         "PalArray": [1.0, 2.0, 3.0]},
        {"ID": 8, "Packed": 1, "Signed": 511, "Pallet": 1234567,
         "PalArray": [4.0, 5.0, 6.0]},
        {"ID": 9, "Packed": 0, "Signed": -512, "Pallet": 7654321,
         "PalArray": [1.0, 2.0, 3.0]},
    ]
    table = parse_db2(DF.build_wdc3(columns, rows), "P.db2",
                      index_for(defs_dir, "P", columns), "P")
    assert table.rows[7]["Packed"] == 4095
    assert table.rows[7]["Signed"] == -300 and table.rows[9]["Signed"] == -512
    assert table.rows[7]["Common"] == 42       # listed in the side table
    assert table.rows[8]["Common"] == 99       # falls back to the default
    assert table.rows[9]["Pallet"] == 7654321
    assert table.rows[8]["PalArray"] == [4.0, 5.0, 6.0]
    # Rows with identical array values share one palette slot.
    assert table.rows[9]["PalArray"] == [1.0, 2.0, 3.0]


def test_id_list_and_copy_table(defs_dir):
    columns = [DF.Col("ID", "int", 32, is_id=True, non_inline=True),
               DF.Col("Value", "int", 32), DF.Col("Name", "string", 32)]
    rows = [{"ID": 500, "Value": 11, "Name": "alpha"},
            {"ID": 501, "Value": 22, "Name": "beta"}]
    raw = DF.build_wdc3(columns, rows, use_id_list=True,
                        copies=[(900, 500), (901, 501)])
    table = parse_db2(raw, "I.db2", index_for(defs_dir, "I", columns), "I")
    assert sorted(table.rows) == [500, 501, 900, 901]
    assert table.rows[900]["Value"] == 11 and table.rows[900]["Name"] == "alpha"
    assert table.rows[900]["ID"] == 900     # the clone carries its own id


def test_relationship_column(defs_dir):
    columns = [DF.Col("ID", "int", 32, is_id=True), DF.Col("V", "int", 32)]
    text = DF.build_dbd("Rel", columns, "1FE1BDA4")
    text = text.replace("COLUMNS\nint ID", "COLUMNS\nint ID\nint Owner")
    text = text.replace("$id$ID<32>", "$id$ID<32>\n$relation$Owner<32>")
    (defs_dir / "Rel.dbd").write_text(text)
    raw = DF.build_wdc3(columns, [{"ID": 1, "V": 5}, {"ID": 2, "V": 6}],
                        relationship={0: 777, 1: 888})
    table = parse_db2(raw, "Rel.db2", DbdIndex(defs_dir), "Rel")
    assert table.rows[1]["Owner"] == 777 and table.rows[2]["Owner"] == 888


def test_encrypted_sections_are_reported_not_guessed(defs_dir):
    raw = DF.build_wdc3(BASIC_COLUMNS, BASIC_ROWS, encrypted=True)
    table = parse_db2(raw, "E.db2", index_for(defs_dir, "Basic", BASIC_COLUMNS),
                      "Basic")
    assert table.encrypted_sections == 1
    assert table.skipped_records == 2
    assert not table.rows


def test_columns_stay_readable_without_a_definition():
    table = parse_db2(DF.build_wdc3(BASIC_COLUMNS, BASIC_ROWS), "B.db2", None,
                      "Basic")
    assert not table.named
    assert table.column_names()[0] == "field_0"
    assert len(table.rows) == 2


def test_a_definition_with_the_wrong_column_count_falls_back(defs_dir):
    short = BASIC_COLUMNS[:3]
    (defs_dir / "Basic.dbd").write_text(DF.build_dbd("Basic", short, "1FE1BDA4"))
    table = parse_db2(DF.build_wdc3(BASIC_COLUMNS, BASIC_ROWS), "B.db2",
                      DbdIndex(defs_dir), "Basic")
    assert not table.named          # rather than mislabelling the columns


def test_wdc1_keeps_its_section_bookkeeping_in_the_header(defs_dir):
    """WDC1 has no section table, so its id list and copy table come from the
    header; reading them from an empty section header would drop both."""
    columns = [DF.Col("ID", "int", 32, is_id=True, non_inline=True),
               DF.Col("V", "int", 32)]
    raw = DF.build_wdc3(columns, [{"ID": 50, "V": 5}, {"ID": 51, "V": 6}],
                        magic="WDC1", use_id_list=True, copies=[(900, 50)])
    table = parse_db2(raw, "U.db2", index_for(defs_dir, "U", columns), "U")
    assert sorted(table.rows) == [50, 51, 900]
    assert table.rows[900]["V"] == 5


@pytest.mark.parametrize("magic", [b"WDB2", b"WDB3", b"WDB4", b"WDB5", b"WDB6"])
def test_pre_wdc_formats_are_refused_rather_than_misread(magic):
    """They lay records out differently enough that reading one as a WDC gives
    plausible wrong values instead of an error."""
    with pytest.raises(UnsupportedFormatError, match="WDC1 and later"):
        parse_db2(magic + b"\0" * 200, "old.db2")


def test_a_truncated_file_fails_instead_of_returning_partial_rows(defs_dir):
    raw = DF.build_wdc3(BASIC_COLUMNS, BASIC_ROWS)
    index = index_for(defs_dir, "Basic", BASIC_COLUMNS)
    with pytest.raises(MalformedFileError, match="but the file is"):
        parse_db2(raw[:-20], "Basic.db2", index, "Basic")


def test_a_non_database_is_rejected():
    with pytest.raises(UnsupportedFormatError):
        parse_db2(b"NOPE" + b"\0" * 100, "x.db2")


# ---------------------------------------------------------------------------
# Sparse (offset map) tables
# ---------------------------------------------------------------------------
SPARSE_COLUMNS = [
    DF.Col("ID", "int", 32, is_id=True),
    DF.Col("V", "int", 32),
    DF.Col("Name", "string", 32),
    DF.Col("Tag", "string", 32),
]
SPARSE_ROWS = [
    {"ID": 100, "V": 7, "Name": "alpha", "Tag": "x"},
    {"ID": 250, "V": 9, "Name": "a much longer name", "Tag": "yy"},
    {"ID": 9001, "V": 11, "Name": "z", "Tag": ""},
]


def test_sparse_tables_decode_variable_records_with_inline_strings(defs_dir):
    raw = DF.build_sparse_wdc3(SPARSE_COLUMNS, SPARSE_ROWS)
    table = parse_db2(raw, "S.db2", index_for(defs_dir, "S", SPARSE_COLUMNS), "S")
    assert sorted(table.rows) == [100, 250, 9001]
    assert table.rows[250]["Name"] == "a much longer name"
    assert table.rows[9001]["Name"] == "z" and table.rows[9001]["Tag"] == ""
    assert table.rows[100]["V"] == 7


def test_a_definition_that_does_not_fit_a_sparse_record_fails(defs_dir):
    """Walking the columns has to land exactly on the end of the record."""
    raw = DF.build_sparse_wdc3(SPARSE_COLUMNS, SPARSE_ROWS)
    short = [DF.Col("ID", "int", 32, is_id=True), DF.Col("V", "int", 32)]
    index = index_for(defs_dir, "S", short)
    with pytest.raises(MalformedFileError, match="does not match this table"):
        parse_db2(raw, "S.db2", index, "S")


def test_a_corrupt_sparse_offset_map_fails(defs_dir):
    raw = bytearray(DF.build_sparse_wdc3(SPARSE_COLUMNS, SPARSE_ROWS))
    map_at = len(raw) - len(SPARSE_ROWS) * 4 - len(SPARSE_ROWS) * 6
    struct.pack_into("<IH", raw, map_at, 999999, 8)
    index = index_for(defs_dir, "S", SPARSE_COLUMNS)
    with pytest.raises(MalformedFileError, match="outside the record region"):
        parse_db2(bytes(raw), "S.db2", index, "S")


def test_inspect_db2_describes_the_table(defs_dir):
    info = inspect_db2(DF.build_wdc3(BASIC_COLUMNS, BASIC_ROWS), "Basic.db2",
                       index_for(defs_dir, "Basic", BASIC_COLUMNS))
    assert info["magic"] == "WDC3" and info["rows"] == 2
    assert info["wotlk_compatible"] is False


# ---------------------------------------------------------------------------
# DBC
# ---------------------------------------------------------------------------
def test_dbc_round_trips_every_field_type():
    raw = DF.build_dbc(4, [[7, -3, 1.5, "hello"]],
                       types=["uint", "int", "float", "string"])
    table = DbcTable.parse(raw, "t.dbc")
    assert table.field_count == 4 and len(table) == 1
    assert table.value(0, 0, "uint") == 7
    assert table.value(0, 1, "int") == -3
    assert table.value(0, 2, "float") == pytest.approx(1.5)
    assert table.value(0, 3, "string") == "hello"
    assert table.serialize() == raw


def test_a_non_dbc_is_rejected():
    with pytest.raises(UnsupportedFormatError):
        DbcTable.parse(b"WDC3" + b"\0" * 40, "x.dbc")


def test_a_record_size_that_disagrees_with_the_field_count_is_rejected():
    raw = bytearray(DF.build_dbc(2, [[1, 2]]))
    struct.pack_into("<I", raw, 12, 99)
    with pytest.raises(MalformedFileError, match="record size"):
        DbcTable.parse(bytes(raw), "x.dbc")


def test_builder_appends_without_disturbing_the_template():
    template = DbcTable.parse(
        DF.build_dbc(3, [[1, 0, "first"], [2, 0, "second"]],
                     types=["uint", "uint", "string"]), "t.dbc")
    builder = DbcBuilder(3, template)
    builder.index_existing(0)
    assert builder.add({0: ("uint", 9), 2: ("string", "third")}, 9) is True
    result = DbcTable.parse(builder.serialize(), "o.dbc")
    assert len(result) == 3
    # Existing string offsets still resolve: the block was appended to, not
    # rebuilt, so the template's rows never need re-interpreting.
    assert result.value(0, 2, "string") == "first"
    assert result.value(1, 2, "string") == "second"
    assert result.value(2, 2, "string") == "third"


def test_builder_replaces_a_row_with_the_same_id():
    template = DbcTable.parse(DF.build_dbc(2, [[5, 50], [6, 60]]), "t.dbc")
    builder = DbcBuilder(2, template)
    builder.index_existing(0)
    assert builder.add({0: ("uint", 5), 1: ("uint", 999)}, 5) is False
    result = DbcTable.parse(builder.serialize(), "o.dbc")
    assert len(result) == 2 and result.value(0, 1, "uint") == 999


def test_a_template_of_the_wrong_width_is_refused():
    template = DbcTable.parse(DF.build_dbc(5, [[1, 2, 3, 4, 5]]), "t.dbc")
    with pytest.raises(MalformedFileError, match="template has 5 fields"):
        DbcBuilder(6, template)


def test_writing_past_the_last_field_is_refused():
    with pytest.raises(MalformedFileError, match="outside"):
        DbcBuilder(2).encode({5: ("uint", 1)})


def test_inspect_dbc():
    info = inspect_dbc(DF.build_dbc(3, [[1, 2, 3]]), "t.dbc")
    assert info["fields"] == 3 and info["records"] == 1
    assert info["wotlk_compatible"] is True


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------
def test_builtin_mappings_load_and_are_self_consistent():
    library = MappingLibrary()
    assert set(library.tables()) >= {"CreatureDisplayInfo", "CreatureModelData",
                                     "GameObjectDisplayInfo", "ItemDisplayInfo"}
    for name in library.tables():
        mapping = library.get(name)
        indices = [c.index for c in mapping.columns]
        assert len(indices) == len(set(indices)), f"{name} maps a field twice"
        assert max(indices) < mapping.target_field_count
        for column in mapping.columns:
            assert column.source or column.const is not None


def test_a_mapping_that_writes_one_field_twice_is_rejected():
    with pytest.raises(ConversionError, match="mapped twice"):
        TableMapping.from_dict({"table": "T", "target_field_count": 4, "columns": [
            {"index": 1, "type": "uint", "const": 0},
            {"index": 1, "type": "uint", "const": 1}]}, "test")


def test_a_mapping_reaching_past_the_table_is_rejected():
    with pytest.raises(ConversionError, match="only has 2 fields"):
        TableMapping.from_dict({"table": "T", "target_field_count": 2, "columns": [
            {"index": 5, "type": "uint", "const": 0}]}, "test")


def test_a_column_with_neither_source_nor_constant_is_rejected():
    with pytest.raises(ConversionError, match="neither"):
        TableMapping.from_dict({"table": "T", "target_field_count": 2,
                                "columns": [{"index": 0, "type": "uint"}]}, "t")


def test_an_unknown_field_type_is_rejected():
    with pytest.raises(ConversionError, match="unknown type"):
        TableMapping.from_dict({"table": "T", "target_field_count": 2, "columns": [
            {"index": 0, "type": "decimal", "const": 0}]}, "test")


def test_transforms_produce_the_spellings_the_client_wants():
    listfile = Listfile()
    listfile.update(["10;creature/bear/bear.m2", "11;creature/bear/skin.blp"])
    ctx = TransformContext(listfile)
    mapping = TableMapping.from_dict({
        "table": "T", "target_field_count": 4, "columns": [
            {"index": 0, "type": "string", "from": "M", "transform": "model_path"},
            {"index": 1, "type": "string", "from": "T", "transform": "basename"},
            {"index": 2, "type": "string", "from": "M", "transform": "path"},
            {"index": 3, "type": "float", "from": "S", "scale": 2.0},
        ]}, "test")
    out = apply_row(mapping, {"M": 10, "T": 11, "S": 1.5}, ctx)
    # DBC model paths take the .mdx spelling; the client swaps it for .m2.
    assert out[0] == ("string", "creature\\bear\\bear.mdx")
    assert out[1] == ("string", "skin")
    assert out[2] == ("string", "creature\\bear\\bear.m2")
    assert out[3] == ("float", pytest.approx(3.0))


def test_unresolved_file_ids_are_recorded():
    ctx = TransformContext(Listfile())
    mapping = TableMapping.from_dict({
        "table": "T", "target_field_count": 1, "columns": [
            {"index": 0, "type": "string", "from": "M", "transform": "path"}]},
        "test")
    assert apply_row(mapping, {"M": 4242}, ctx)[0] == ("string", "")
    assert ctx.missing == {4242}


def test_first_present_source_wins():
    mapping = TableMapping.from_dict({
        "table": "T", "target_field_count": 1, "columns": [
            {"index": 0, "type": "uint", "from": ["New", "Old"]}]}, "test")
    ctx = TransformContext()
    assert apply_row(mapping, {"Old": 5}, ctx)[0] == ("uint", 5)
    assert apply_row(mapping, {"New": 9, "Old": 5}, ctx)[0] == ("uint", 9)


def test_missing_sources_are_listed():
    mapping = TableMapping.from_dict({
        "table": "T", "target_field_count": 2, "columns": [
            {"index": 0, "type": "uint", "from": "Here"},
            {"index": 1, "type": "uint", "from": "Gone"}]}, "test")
    assert [c.index for c in missing_sources(mapping, ["Here"])] == [1]


def test_user_mappings_take_precedence(tmp_path):
    override = {"table": "CreatureModelData", "target_field_count": 3,
                "verified": True,
                "columns": [{"index": 0, "type": "uint", "from": "ID"}]}
    (tmp_path / "CreatureModelData.json").write_text(json.dumps(override))
    library = MappingLibrary(tmp_path)
    assert library.get("CreatureModelData").target_field_count == 3


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------
MODEL_COLUMNS = [
    DF.Col("ID", "int", 32, is_id=True),
    DF.Col("FileDataID", "int", 32),
    DF.Col("Flags", "int", 32),
    DF.Col("ModelScale", "float", 32),
    DF.Col("CollisionHeight", "float", 32),
    DF.Col("GeoBoxMin", "float", 32, array=3),
    DF.Col("GeoBoxMax", "float", 32, array=3),
]
MODEL_ROWS = [
    {"ID": 5001, "FileDataID": 1394961, "Flags": 0, "ModelScale": 1.25,
     "CollisionHeight": 3.0, "GeoBoxMin": [-2.0, -2.0, 0.0],
     "GeoBoxMax": [2.0, 2.0, 4.0]},
    {"ID": 5002, "FileDataID": 1394970, "Flags": 1, "ModelScale": 0.5,
     "CollisionHeight": 1.2, "GeoBoxMin": [-1.0, -1.0, 0.0],
     "GeoBoxMax": [1.0, 1.0, 2.0]},
]


@pytest.fixture
def model_listfile():
    listfile = Listfile()
    listfile.update(["1394961;creature/bear/bear.m2",
                     "1394970;creature/wolf/wolf.m2"])
    return listfile


@pytest.fixture
def model_db2(defs_dir):
    index = index_for(defs_dir, "CreatureModelData", MODEL_COLUMNS)
    return DF.build_wdc3(MODEL_COLUMNS, MODEL_ROWS), index


def test_table_name_comes_from_the_path():
    assert table_name_for("dbfilesclient\\CreatureModelData.db2") == \
        "CreatureModelData"
    assert table_name_for("/tmp/x/ItemDisplayInfo.db2") == "ItemDisplayInfo"


def test_fresh_conversion_writes_the_mapped_fields(model_db2, model_listfile):
    raw, index = model_db2
    out, res = convert_db2(raw, "CreatureModelData.db2", Options(),
                           model_listfile, index)
    assert res.ok
    table = DbcTable.parse(out, "o.dbc")
    assert table.field_count == 28 and len(table) == 2
    assert table.value(0, 0, "uint") == 5001
    assert table.value(0, 2, "string") == "creature\\bear\\bear.mdx"
    assert table.value(0, 4, "float") == pytest.approx(1.25)
    assert table.value(0, 15, "float") == pytest.approx(3.0)
    assert table.value(0, 17, "float") == pytest.approx(-2.0)
    assert table.value(1, 2, "string") == "creature\\wolf\\wolf.mdx"


def test_merging_keeps_existing_rows_and_offsets_new_ids(model_db2,
                                                         model_listfile):
    raw, index = model_db2
    template = DF.build_dbc(28, [
        [1, 0, "creature\\murloc\\murloc.mdx"] + [0] * 25,
        [2, 0, "creature\\kobold\\kobold.mdx"] + [0] * 25,
    ], types=["uint", "uint", "string"] + ["uint"] * 25)
    out, res = convert_db2(raw, "CreatureModelData.db2",
                           Options(db_id_offset=100000), model_listfile, index,
                           template_data=template)
    assert res.ok and res.extra["rows_kept"] == 2 and res.extra["rows_added"] == 2
    table = DbcTable.parse(out, "o.dbc")
    assert len(table) == 4
    assert table.value(0, 2, "string") == "creature\\murloc\\murloc.mdx"
    assert table.value(2, 0, "uint") == 105001
    assert table.value(2, 2, "string") == "creature\\bear\\bear.mdx"


def test_a_template_of_the_wrong_width_fails_the_file(model_db2, model_listfile):
    raw, index = model_db2
    out, res = convert_db2(raw, "CreatureModelData.db2", Options(),
                           model_listfile, index,
                           template_data=DF.build_dbc(27, [[1] + [0] * 26]))
    assert res.status is Status.FAILED and out == b""
    message = next(n.message for n in res.notes if n.level == "error")
    assert "27 fields" in message and "28" in message


def test_without_a_template_the_unverified_layout_is_flagged(model_db2,
                                                             model_listfile):
    raw, index = model_db2
    _out, res = convert_db2(raw, "CreatureModelData.db2", Options(),
                            model_listfile, index)
    assert any(n.code == "db2.unverified_layout" for n in res.notes)


def test_a_template_silences_the_layout_warning(model_db2, model_listfile):
    raw, index = model_db2
    _out, res = convert_db2(raw, "CreatureModelData.db2", Options(),
                            model_listfile, index,
                            template_data=DF.build_dbc(28, [[1] + [0] * 27]))
    codes = {n.code for n in res.notes}
    assert "db2.unverified_layout" not in codes and "db2.template" in codes


def test_columns_this_build_lacks_are_reported(model_db2, model_listfile):
    raw, index = model_db2
    _out, res = convert_db2(raw, "CreatureModelData.db2", Options(),
                            model_listfile, index)
    note = next(n for n in res.notes if n.code == "db2.columns_absent")
    assert "SoundID" in note.message or "BloodID" in note.message


def test_conversion_without_a_definition_fails_with_advice(model_listfile):
    raw = DF.build_wdc3(MODEL_COLUMNS, MODEL_ROWS)
    _out, res = convert_db2(raw, "CreatureModelData.db2", Options(),
                            model_listfile, None)
    assert res.status is Status.FAILED
    assert "--dbd" in next(n.message for n in res.notes if n.level == "error")


def test_a_table_with_no_mapping_says_which_ones_exist(defs_dir,
                                                       model_listfile):
    index = index_for(defs_dir, "SpellMisc", MODEL_COLUMNS)
    raw = DF.build_wdc3(MODEL_COLUMNS, MODEL_ROWS)
    with pytest.raises(MissingDependencyError, match="CreatureModelData"):
        convert_db2(raw, "SpellMisc.db2", Options(), model_listfile, index)


def test_row_selection_by_id(model_db2, model_listfile):
    raw, index = model_db2
    out, res = convert_db2(raw, "CreatureModelData.db2",
                           Options(db_row_ids=(5002,)), model_listfile, index)
    assert res.extra["rows_added"] == 1
    assert DbcTable.parse(out, "o").value(0, 0, "uint") == 5002


def test_row_selection_by_column_value(model_db2, model_listfile):
    raw, index = model_db2
    out, _res = convert_db2(raw, "CreatureModelData.db2",
                            Options(db_where=(("Flags", "1"),)),
                            model_listfile, index)
    table = DbcTable.parse(out, "o")
    assert len(table) == 1 and table.value(0, 0, "uint") == 5002


def test_unresolved_file_ids_are_reported(model_db2):
    raw, index = model_db2
    _out, res = convert_db2(raw, "CreatureModelData.db2", Options(),
                            Listfile(), index)
    assert any(n.code == "db2.unresolved_files" for n in res.notes)


def test_encrypted_rows_are_reported_by_the_converter(defs_dir,
                                                      model_listfile):
    index = index_for(defs_dir, "CreatureModelData", MODEL_COLUMNS)
    raw = DF.build_wdc3(MODEL_COLUMNS, MODEL_ROWS, encrypted=True)
    _out, res = convert_db2(raw, "CreatureModelData.db2", Options(),
                            model_listfile, index)
    assert any(n.code == "db2.encrypted" for n in res.notes)


def test_find_template_is_case_insensitive(tmp_path):
    (tmp_path / "creaturemodeldata.dbc").write_bytes(DF.build_dbc(2, [[1, 2]]))
    assert find_template(tmp_path, "CreatureModelData") is not None
    assert find_template(tmp_path, "Missing") is None
    assert find_template(None, "CreatureModelData") is None


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------
@pytest.fixture
def db_cli_tree(tmp_path):
    """A directory laid out the way a user would invoke ``db convert`` on."""
    defs = tmp_path / "definitions"
    defs.mkdir()
    (defs / "CreatureModelData.dbd").write_text(
        DF.build_dbd("CreatureModelData", MODEL_COLUMNS, "1FE1BDA4"))
    (tmp_path / "CreatureModelData.db2").write_bytes(
        DF.build_wdc3(MODEL_COLUMNS, MODEL_ROWS))

    client = tmp_path / "client"
    client.mkdir()
    (client / "CreatureModelData.dbc").write_bytes(DF.build_dbc(
        28, [[1, 0, "creature\\murloc\\murloc.mdx"] + [0] * 25],
        types=["uint", "uint", "string"] + ["uint"] * 25))

    (tmp_path / "listfile.csv").write_text(
        "1394961;creature/bear/bear.m2\n1394970;creature/wolf/wolf.m2\n")
    return tmp_path


def test_cli_db_tables_lists_the_builtins(capsys):
    from wotlkconv.cli import main
    assert main(["db", "tables"]) == 0
    out = capsys.readouterr().out
    assert "CreatureModelData" in out and "CreatureDisplayInfo" in out
    # every built-in is flagged as unchecked against a real client
    assert "UNVERIFIED" in out


def test_cli_db_tables_can_show_columns(capsys):
    from wotlkconv.cli import main
    assert main(["db", "tables", "--verbose-columns"]) == 0
    assert "<- ID" in capsys.readouterr().out


def test_cli_db_convert_merges_onto_the_template(db_cli_tree, tmp_path):
    from wotlkconv.cli import main
    out = tmp_path / "out"
    code = main(["db", "convert", str(db_cli_tree / "CreatureModelData.db2"),
                 "-o", str(out), "--dbd", str(db_cli_tree / "definitions"),
                 "-l", str(db_cli_tree / "listfile.csv"),
                 "--template-dir", str(db_cli_tree / "client"),
                 "--id-offset", "200000"])
    assert code == 0
    table = DbcTable.parse((out / "CreatureModelData.dbc").read_bytes(), "o")
    assert len(table) == 3                       # one kept, two added
    assert table.value(0, 2, "string") == "creature\\murloc\\murloc.mdx"
    assert table.value(1, 0, "uint") == 205001
    assert table.value(1, 2, "string") == "creature\\bear\\bear.mdx"


def test_cli_db_convert_without_definitions_explains_itself(db_cli_tree,
                                                            tmp_path,
                                                            monkeypatch):
    """Columns have no names without a DBD, so the run must refuse."""
    from wotlkconv.cli import main
    from wotlkconv.db import dbd
    monkeypatch.delenv(dbd.ENV_VAR, raising=False)
    # Somewhere with no definitions/ folder for discovery to find.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert main(["db", "convert", str(db_cli_tree / "CreatureModelData.db2"),
                 "-o", str(elsewhere / "out")]) == 1


def test_definitions_are_discovered_from_the_working_directory(db_cli_tree,
                                                               tmp_path,
                                                               monkeypatch):
    """A 'definitions' folder beside the work is picked up without --dbd."""
    from wotlkconv.cli import main
    from wotlkconv.db import dbd
    monkeypatch.delenv(dbd.ENV_VAR, raising=False)
    monkeypatch.chdir(db_cli_tree)
    assert main(["db", "convert", "CreatureModelData.db2",
                 "-o", str(tmp_path / "out")]) == 0


def test_cli_db_convert_selects_rows(db_cli_tree, tmp_path):
    from wotlkconv.cli import main
    out = tmp_path / "out"
    assert main(["db", "convert", str(db_cli_tree / "CreatureModelData.db2"),
                 "-o", str(out), "--dbd", str(db_cli_tree / "definitions"),
                 "-l", str(db_cli_tree / "listfile.csv"),
                 "--only-id", "5002"]) == 0
    table = DbcTable.parse((out / "CreatureModelData.dbc").read_bytes(), "o")
    assert len(table) == 1 and table.value(0, 0, "uint") == 5002


def test_cli_db_convert_filters_by_column(db_cli_tree, tmp_path):
    from wotlkconv.cli import main
    out = tmp_path / "out"
    assert main(["db", "convert", str(db_cli_tree / "CreatureModelData.db2"),
                 "-o", str(out), "--dbd", str(db_cli_tree / "definitions"),
                 "-l", str(db_cli_tree / "listfile.csv"),
                 "--where", "Flags=1"]) == 0
    table = DbcTable.parse((out / "CreatureModelData.dbc").read_bytes(), "o")
    assert len(table) == 1 and table.value(0, 0, "uint") == 5002


def test_cli_db_convert_refuses_a_mismatched_template(db_cli_tree, tmp_path):
    from wotlkconv.cli import main
    bad = db_cli_tree / "bad"
    bad.mkdir()
    (bad / "CreatureModelData.dbc").write_bytes(DF.build_dbc(27, [[1] + [0] * 26]))
    assert main(["db", "convert", str(db_cli_tree / "CreatureModelData.db2"),
                 "-o", str(tmp_path / "out"),
                 "--dbd", str(db_cli_tree / "definitions"),
                 "--template-dir", str(bad)]) == 1


def test_cli_db_convert_keeps_an_existing_file_without_overwrite(db_cli_tree,
                                                                 tmp_path):
    from wotlkconv.cli import main
    out = tmp_path / "out"
    out.mkdir()
    (out / "CreatureModelData.dbc").write_bytes(b"SENTINEL")
    args = ["db", "convert", str(db_cli_tree / "CreatureModelData.db2"),
            "-o", str(out), "--dbd", str(db_cli_tree / "definitions"),
            "-l", str(db_cli_tree / "listfile.csv")]
    assert main(args) == 0
    assert (out / "CreatureModelData.dbc").read_bytes() == b"SENTINEL"
    assert main(args + ["--overwrite"]) == 0
    assert (out / "CreatureModelData.dbc").read_bytes() != b"SENTINEL"


def test_cli_db_convert_reports_a_missing_file(tmp_path, db_cli_tree):
    from wotlkconv.cli import main
    assert main(["db", "convert", str(tmp_path / "nope.db2"),
                 "-o", str(tmp_path / "out"),
                 "--dbd", str(db_cli_tree / "definitions")]) == 1


def test_a_malformed_where_clause_is_rejected():
    from wotlkconv.cli import _parse_where
    from wotlkconv.errors import ConverterError
    assert _parse_where(["Flags=1", "Name=bear"]) == [("Flags", "1"),
                                                      ("Name", "bear")]
    with pytest.raises(ConverterError, match="COLUMN=VALUE"):
        _parse_where(["Flags"])
