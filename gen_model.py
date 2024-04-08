import io
import pydash
from typing import Any, cast
import yaml

StructList = list[tuple[str, dict[str, Any]]]

class StringBuilder(object):

    def __init__(self):
        self._stringio = io.StringIO()
    
    def __str__(self):
        return self._stringio.getvalue()
    
    def append(self, *objects, sep=' ', end=''):
        print(*objects, sep=sep, end=end, file=self._stringio)

def make_pascal_case(str: str) -> str:
    if str.endswith("es"):
        str = str[:-2]
    elif str.endswith("s"):
        str = str[:-1]
    a = str.split("_")
    res = []
    for b in a:
        b = b.title()
        res.append(b)
    return "".join(res)

keywords = set(['False', 'None', 'True', 'and', 'as', 'assert', 'async', 'await', 'break', 
           'class', 'continue', 'def', 'del', 'elif', 'else', 'except', 'finally', 'for', 
           'from', 'global', 'if', 'import', 'in', 'is', 'lambda', 'nonlocal', 'not', 'or', 
           'pass', 'raise', 'return', 'try', 'while', 'with', 'yield'])

def make_valid_name(str: str) -> str:
    if str in keywords:
        return f"{str}_"
    return str

def make_unique_name(struct_names: set[str], name: str) -> str:
    n = name
    i = 1
    while n in struct_names:
        n = name + str(i)
        i = i + 1
    return n

def load_schema(file_name: str) -> dict[str, Any]:
    with open(file_name, "r") as f:
        schema = yaml.load(f, Loader=yaml.FullLoader)
    return schema

def get_nested_object(schema: dict[str, Any]) -> dict[str, Any]|None:
    if schema.get("type") != "object":
        return None
    obj: dict[str, Any] = schema
    while "additionalProperties" in obj and isinstance(obj["additionalProperties"], dict):
        obj = cast(dict[str, Any], obj["additionalProperties"])
    if obj.get("type") == "object" and "properties" in obj:
        return obj
    return None

def recursive_get_structs(root: dict[str, Any],
                          schema: dict[str, Any], 
                          structs: list[tuple[str, dict[str, Any]]], 
                          struct_names: set[str]) -> None:
    obj: dict[str, Any]|None = get_nested_object(schema)
    if obj is None:
        return
    props: dict[str, Any] = obj["properties"]
    # First pass - recurse into any sub structs or arrays
    for key, value in props.items():
        obj = get_nested_object(value)
        if obj:
            recursive_get_structs(root, obj, structs, struct_names)
        elif value.get("type") == "array" and "items" in value:
            if value["items"]["type"] == "object":
                recursive_get_structs(root, value["items"], structs, struct_names)
    # Second pass - add any sub structs
    for key, value in props.items():
        obj = get_nested_object(value)
        if obj:
            struct_name = make_unique_name(struct_names, make_pascal_case(key))
            value["struct_name"] = struct_name
            struct_names.add(struct_name)
            structs.append((struct_name, obj))
    # Third pass - shallow expand references
    for key, value in props.items():
        obj = get_nested_object(value)
        if obj is not None:
            value = obj
        if "$ref" in value:
            ref_name = value["$ref"].split("/")[-1]
            ref = root["definitions"][ref_name]
            value.update(ref)
        if value.get("type") == "array" and "items" in value and "$ref" in value["items"]:
            ref_name = value["items"]["$ref"].split("/")[-1]
            ref = root["definitions"][ref_name]
            value["items"].update(ref)

def get_structs(base_struct_name: str, schema: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    structs: list[tuple[str, dict[str, Any]]] = []
    struct_names: set[str] = set()
    if "definitions" in schema:
        defs: dict[str, Any] = schema["definitions"]
        # First pass just add all definition structs (they get first pick of names)
        for key, value in defs.items():
            # All top level definition names are unique
            recursive_get_structs(schema, value, structs, struct_names)
            struct_name = make_pascal_case(key)
            struct_names.add(struct_name)
            structs.append((struct_name, value))
    recursive_get_structs(schema, schema, structs, struct_names)
    struct_names.add(base_struct_name)
    structs.append((base_struct_name, schema))
    return structs

def write_model_classes(base_struct_name: str, file_name: str, out_file_name: str) -> None:
    schema = load_schema(file_name)
    structs: list[tuple[str, dict[str, Any]]] = get_structs(base_struct_name, schema)
    sb = StringBuilder()
    sb.append(
"""# Auto generated file - do not modify.
from typing import Any, Iterator, TypeVar

class ModelBase:

    def __init__(self, dd: dict[str, Any]|None = None) -> None:
        self._dd = dd if dd else {}

    @property
    def dd(self) -> dict[str, Any]:
        return self._dd
    @dd.setter
    def dd(self, dd: dict[str, Any]) -> None:
        self._dd = dd

    def dd_has(self, name: str) -> bool:
        return name in self._dd            

    def dd_delete(self, name: str) -> None:
        del self._dd[name]

T = TypeVar('T', bound=ModelBase)

def dd_items(d: dict[str, Any], m: T) -> Iterator[tuple[str, T]]:
    \"\"\"Iterate key value pairs of a dictionary.\"\"\"
    for k, v in d.items():
        m.dd = v
        yield (k, m)

def dd_values(d: dict[str, Any], m: T) -> Iterator[T]:
    \"\"\"Iterate values of a dictionary.\"\"\"
    for k, v in d.items():
        m.dd = v
        yield m
        
def dd_elems(l: list[dict[str, Any]], m: T) -> Iterator[T]:
    \"\"\"Iterate elements of an array.\"\"\"
    for e in l:
        m.dd = e
        yield m
        
""")
    for struct_tuple in structs:
        struct_name, struct = struct_tuple
        obj: dict[str, Any] = get_nested_object(struct) or { "properties": {} }
        props: dict[str, Any] = obj["properties"]
        if len(props) == 0:
            continue
        sb.append(
f"""class {struct_name}(ModelBase):
""")
        for prop_name, prop_def in props.items():
            prop_name = make_valid_name(prop_name)
            prop_type = prop_def["type"]
            struct_name = ""
            if prop_type == "object" and prop_def.get("additionalProperties") == False:
                if "struct_name" in prop_def:
                    struct_name = prop_def["struct_name"]
                    sb.append(
f"""    @property
    def {prop_name}(self) -> {struct_name}:
        return {struct_name}(self._dd.get("{prop_name}", {{}}))
    @{prop_name}.setter
    def {prop_name}(self, v: {struct_name}) -> None:
        self._dd["{prop_name}"] = None if v is None else v.dd

""")                
                    continue            
            py_ary_type = ""
            if prop_type == "array":
                ary_type = prop_def["items"]["type"]
                py_ary_type = { "string": "str", "number": "float", "integer": "int", "boolean": "bool", "object": "dict[str, Any]", "array": "list[Any]" }[ary_type]
            py_type = { "string": "str", "number": "float", "integer": "int", "boolean": "bool", "object": "dict[str, Any]", "array": f"list[{py_ary_type}]" }[prop_type]
            py_default = { "string": "\"\"", "number": "0.0", "integer": 0, "boolean": "False", "object": "{}", "array": "[]" }[prop_type]
            sb.append(
f"""    @property
    def {prop_name}(self) -> {py_type}:
        return self._dd.get("{prop_name}", {py_default})
    @{prop_name}.setter
    def {prop_name}(self, v: {py_type}) -> None:
        self._dd["{prop_name}"] = v

""")
    text = str(sb)
    with open(out_file_name, "w") as f:
        f.write(text)

write_model_classes("Module", "schemas/module_schema_hoa.yaml", "src/games/hoa/models_hoa.py")