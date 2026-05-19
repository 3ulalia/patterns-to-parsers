import os
import sys
import json
import math
import argparse
import re
import itertools
import jinja2 as jj
from collections import defaultdict
from typing import Optional

# -----------------------------------------------------------------------------
# 1. Environment & Jinja2 Setup
# -----------------------------------------------------------------------------

# Get the absolute path of the 'utils' directory
UTILS_DIR = os.path.dirname(os.path.abspath(__file__))
# Get the project root (one level up from utils)
BASE_DIR = os.path.dirname(UTILS_DIR)
# Define templates directory
TEMPLATE_DIR = os.path.join(BASE_DIR, "templates")

if not os.path.exists(TEMPLATE_DIR):
    # Fallback or warning if templates dir is missing
    print(f"Warning: Template directory not found at {TEMPLATE_DIR}")

jj_env = jj.Environment(
    loader=jj.FileSystemLoader(TEMPLATE_DIR),
    undefined=jj.StrictUndefined
)

def jj_render(template_name: str, module_name: str, context: dict, outfile_dir: str):
    """
    Render a Jinja2 template and save it to a file.
    """
    os.makedirs(outfile_dir, exist_ok=True)
    module_file_path = os.path.join(outfile_dir, f"{module_name}.sv")
    
    try:
        template = jj_env.get_template(template_name)
        stream = template.stream(context)
        stream.dump(module_file_path)
    except jj.TemplateNotFound:
        raise FileNotFoundError(f"Template '{template_name}' not found in {TEMPLATE_DIR}")

def clog2(n):
    return math.ceil(math.log2(n)) if n > 0 else 1

def str_to_hex(s):
    # return f"{len(s)*4}'h" + ''.join(f"{ord(c):X}" for c in s)
    return f"{len(s)*4}'h{s}"

def get_build_rtl_parse_args():
    parser = argparse.ArgumentParser(description="Script to count luts or build RTL from a python like dictionary")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("-c", "--config", type=str, help='Path to a single JSON config file')
    group.add_argument("-cd", "--config_dir", type=str, help='Path to a folder containing JSON config file(s)')
    parser.add_argument("-m", "--mask", type=str, required=True, help='Path to Input file containing Mask and Data')
    parser.add_argument("-o", "--outdir", type=str, required=True, help='Output directory name')
    parser.add_argument("--rtl", action="store_true", help="Generate RTL files")
    # parser.add_argument("--countluts", action="store_true", help="Count number of LUTs to estimate area usage")
    parser.add_argument("--debug", action="store_true", help="Print Debug info")
    parser.add_argument("--tree1", action="store_true", help="Builds a priority tree logic for Stage1: comparison logic, otherwise flat")
    parser.add_argument("--tree2", action="store_true", help="Builds a priority tree logic for Stag2: lookup logic, otherwise flat case")
    parser.add_argument("--unique1", action="store_true", help="Builds a unique case logic for Stage1: comparison logic, otherwise flat")
    parser.add_argument("--unique2", action="store_true", help="Builds a unique case logic for Stag2: lookup logic, otherwise flat case")
    # parser.add_argument("--retime", action="store_true", help="Adds pipe stages to improve timing")
    return parser.parse_args()

def get_top_parse_args():
    parser = argparse.ArgumentParser(description="JSON to RTL generation for Parser")
    parser.add_argument("--g", "--graph", type=str, required=True, help='Graph json file')
    parser.add_argument("--dw", "--datawidth", type=int, required=True, help="Input data width at the top RTL")
    parser.add_argument("--mw", "--maxwidth", type=int, required=True, help="Max Packet width to parse")
    parser.add_argument("--cm", "--colmult", type=int, required=False, default=1, help="Column Multiplier to change comparator sizes")
    parser.add_argument("--o", "--outdir", type=str, required=True, help='Final Output directory name')
    parser.add_argument("--debug", action="store_true", help="Print Debug info")
    return parser.parse_args()

def get_mask_parse_args():
    parser = argparse.ArgumentParser(description="Builds a mask file for Parser RTL generation")
    parser.add_argument("--g", "--graph", type=str, required=True, help='Graph json file')
    parser.add_argument("--o", "--output", type=str, default="out.hex", help='Output file name')
    parser.add_argument("--w", "--width", type=str, required=True, help='Generates mask width in characters')
    parser.add_argument("--extract", action="store_true", help="Generate extraction mapping JSON for fields marked with extract:<str>")
    parser.add_argument("--debug", action="store_true", help="Print Debug info")
    return parser.parse_args()

def get_p4_to_ir_args():
    parser = argparse.ArgumentParser(description="Builds a mask file from a P4 specification")
    parser.add_argument("--p4", type=str, required=True, help='P4 file')
    parser.add_argument("--o", "--output", type=str, default="tmp", help='Output folder name')
    parser.add_argument("--w", "--width", type=str, required=True, help='Generates mask width in characters')
    parser.add_argument("--debug", action="store_true", help="Print Debug info")
    return parser.parse_args()

def get_p4_to_rtl_args():
    parser = argparse.ArgumentParser(description="P4 to RTL generation (P4 -> mask -> SystemVerilog)")
    parser.add_argument("--p4", type=str, required=True, help='Input P4 source file')
    parser.add_argument("--dw", "--datawidth", type=int, required=True, help="Input data width at the top RTL (bits)")
    parser.add_argument("--mw", "--maxwidth", type=int, required=True, help="Max packet width to parse (bits); must be divisible by --dw")
    parser.add_argument("--cm", "--colmult", type=int, required=False, default=1, help="Column multiplier")
    parser.add_argument("--o", "--outdir", type=str, required=True, help='Output directory for generated RTL')
    parser.add_argument("--mode", choices=["tree", "unique"], default="tree",
                        help="Comparison/lookup implementation style: tree (nested if-else) or unique (parallel comparators)")
    parser.add_argument("--debug", action="store_true", help="Print debug info")
    return parser.parse_args()

def adjust_width(s:str, l:int):
    len_s = len(s)
    if (len_s > l):
        return s[:l]
    elif (len_s < l):
        return s + 'x' * (l - len_s)
     
def load_graph_config(config_path):
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"File not found: {config_path}")
    
    with open(config_path, 'r', encoding='utf-8') as f:
        return json.load(f)

def load_mask(mask_path):
    if not os.path.exists(mask_path):
        raise FileNotFoundError(f"Mask file not found: {mask_path}")
    
    with open(mask_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    return lines

def safe_eval(expr: str, context: dict):
    allowed_names = {
        **context,
        **{k: getattr(math, k) for k in dir(math) if not k.startswith("_")}
    }

    tree = ast.parse(expr, mode="eval")

    for node in ast.walk(tree):
        if not isinstance(node, (
            ast.Expression, ast.BinOp, ast.UnaryOp,
            ast.Num, ast.Constant, ast.Name, ast.Call,
            ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow,
            ast.Mod, ast.Load
        )):
            raise ValueError(f"Unsafe expression: {expr}")

        if isinstance(node, ast.Call) and not isinstance(node.func, ast.Name):
            raise ValueError(f"Unsafe function call in: {expr}")

        if isinstance(node, ast.Name) and node.id not in allowed_names:
            raise ValueError(f"Unknown variable '{node.id}' in expression '{expr}'")

    return eval(compile(tree, "<expr>", "eval"), {"__builtins__": {}}, allowed_names)


def dfs_v2(graph, current, path=None, visited=None, extract_data=False, current_pos=0, extraction_list=None):
    """
    Returns list of tuples: (path_dict, list_of_extraction_tuples)
    
    Extraction tuple format: [(start_pos, end_pos, field_id), ...]
    - start_pos: Absolute bit position where field starts
    - end_pos: Absolute bit position where field ends
    - field_id: String identifier from "extract" field in JSON (or None if not extracted)
    
    Supports conditional extraction via "extract_exclude" field:
    - If "extract_exclude" is present, extraction is skipped for those transition values
    - If "extract_exclude" contains values not in "transitions", raises error
    
    Only uses 'transitions' field for graph traversal.
    - val (transition key) can be any string character composition
    - nxt (next node) is a string node name or None for leaf nodes
    """
    if path is None: 
        path = {}
    if visited is None: 
        visited = set()
    if extraction_list is None: 
        extraction_list = []

    if current in visited:
        raise ValueError(f"Cycle detected at node '{current}'")

    node = graph[current]
    visited.add(current)

    num_bits = node.get("num_bits")
    transitions = node.get("transitions")
    deps = node.get("dependency", [])
    
    # Support both old format (extract: 1) and new format (extract: "field_name")
    extract_field = node.get("extract")
    extract_exclude = node.get("extract_exclude", [])
    
    # Validate extract_exclude if present
    if extract_exclude and transitions:
        transition_keys = set(transitions.keys())
        exclude_set = set(extract_exclude)
        invalid_excludes = exclude_set - transition_keys
        if invalid_excludes:
            raise ValueError(
                f"Node '{current}': extract_exclude contains invalid transition values: {invalid_excludes}. "
                f"Valid transitions are: {transition_keys}"
            )
    
    context = {d: path[d]["value"] for d in deps if d in path}

    if num_bits:
        num_bits_eval = [safe_eval(nb, context) if isinstance(nb, str) else nb for nb in num_bits]
    else:
        num_bits_eval = []

    all_paths = []
    all_extractions = []

    # Calculate current node geometry
    bit_width = num_bits_eval[0] if num_bits_eval else 0
    start_pos = current_pos
    end_pos = start_pos + bit_width

    if transitions is not None:
        for val, nxt in transitions.items():
            # val is the transition key (can be any string)
            # nxt is the next node name (string) or None (leaf)

            # Check if this transition should be extracted
            should_extract = extract_data and extract_field and (val not in extract_exclude)
            
            # Build extraction list for this path
            new_extraction_list = extraction_list.copy()
            if should_extract:
                # Support both integer (1) and string field IDs
                field_id = str(extract_field) if extract_field != 1 else current
                new_extraction_list.append((start_pos, end_pos, field_id))

            new_path = {**path}
            new_path[current] = {"value": val, "num_bits": num_bits_eval, "dependency": deps}

            if nxt is None:
                # Leaf node
                all_paths.append(new_path)
                all_extractions.append(new_extraction_list)
            else:
                # Continue traversal to next node
                sub_paths = dfs_v2(graph, nxt, new_path, visited.copy(),
                                extract_data, end_pos, new_extraction_list)
                for p, e in sub_paths:
                    all_paths.append(p)
                    all_extractions.append(e)
    else:
        # Terminal node with no transitions
        # For terminal nodes, extract unconditionally if extract field is present
        new_extraction_list = extraction_list.copy()
        if extract_data and extract_field:
            field_id = str(extract_field) if extract_field != 1 else current
            new_extraction_list.append((start_pos, end_pos, field_id))
        
        new_path = {**path}
        new_path[current] = {"value": None, "num_bits": num_bits_eval, "dependency": deps}
        all_paths.append(new_path)
        all_extractions.append(new_extraction_list)

    return list(zip(all_paths, all_extractions))


def encode_entries(entries, width):
    """
    Encodes entries as character strings.
    Width is interpreted as number of characters.
    Pads with 'x' or truncates based on character count.
    """
    result_strings = []
    
    for entry in entries:
        # Concatenate all values as strings
        char_str = ''
        for key in entry:
            val = entry[key]['value']
            if val is None:
                # Use empty string for None values
                val = ''
            char_str += str(val)
        
        # Pad or truncate based on character width
        if len(char_str) < width:
            # Pad with 'x' characters
            char_str += 'x' * (width - len(char_str))
        elif len(char_str) > width:
            # Truncate to width characters (take last 'width' characters)
            char_str = char_str[-width:]
        
        result_strings.append(char_str)
    
    return result_strings


def resolve_char_per_col(char_per_col, num_cols):
    if isinstance(char_per_col, int):
        return [char_per_col] * num_cols
    elif isinstance(char_per_col, list):
        if len(char_per_col) != num_cols:
            raise ValueError(f"char_per_col must have {num_cols} items.")
        return char_per_col
    else:
        raise TypeError("char_per_col must be int or list of ints.")

def resolve_col_per_file(col_per_file, num_cols):
    """
    Return a list of column counts per file.
    - If col_per_file is already a list: validate and return.
    - If it's an int: split num_cols into chunks and return the list of counts.
    """
    if isinstance(col_per_file, int):
        if col_per_file > num_cols:
            raise ValueError("Sum of col_per_file list must equal num_cols.")
        result = []
        remaining = num_cols
        while remaining > 0:
            count = min(col_per_file, remaining)
            result.append(count)
            remaining -= count
        return result

    elif isinstance(col_per_file, list):
        if sum(col_per_file) != num_cols:
            raise ValueError("Sum of col_per_file list must equal num_cols.")
        return col_per_file

    else:
        raise TypeError("col_per_file must be an int or a list of ints.")


def parse_custom_field(custom):
    """Validate 'custom' is either an empty list or a list of dictionaries.

    Supported formats:

    Port-based entry:
        {
            "id": "YSQVQZIK",
            "type": "port",
            "prefix": "src_ip_range_0",
            "num_ports": 4,          # optional, required only for EQOR
            "opcode": "EQOR"
        }

    Constant-based entry:
        {
            "id": "ABCD1234",
            "type": "const",
            "values": ["<hex>", "<hex>"],
            "opcode": "EQ"
        }
    
    For constant based entry
    #RANGE reads the first two values,
    #NE read the first value only,
    #EQOR reads all the values in the list
    """

    if not custom:
        return []

    for entry in custom:
        # Must be dict
        if not isinstance(entry, dict):
            raise TypeError(f"Each custom entry must be a dict, got {type(entry).__name__}")

        # Common fields
        if "id" not in entry or not isinstance(entry["id"], str):
            raise ValueError("Each entry must have a string 'id'")
        if "type" not in entry or entry["type"] not in ("port", "const"):
            raise ValueError(f"Invalid or missing 'type' in entry {entry['id']}")
        if "opcode" not in entry or not isinstance(entry["opcode"], str):
            raise ValueError(f"Missing or invalid 'opcode' in entry {entry['id']}")

        # Type-specific validation
        if entry["type"] == "port":
            if "prefix" not in entry or not isinstance(entry["prefix"], str):
                raise ValueError(f"Port entry {entry['id']} missing 'prefix'")
            if entry["opcode"] == "EQOR":
                if "num_ports" not in entry or not isinstance(entry["num_ports"], int):
                    raise ValueError(f"Port entry {entry['id']} with opcode EQOR must include integer 'num_ports'")
        elif entry["type"] == "const":
            if "values" not in entry or not isinstance(entry["values"], list) or not entry["values"]:
                raise ValueError(f"Const entry {entry['id']} must include non-empty list of 'values'")
            if not all(isinstance(v, str) for v in entry["values"]):
                raise ValueError(f"Const entry {entry['id']} has non-string constant values")

    return custom


def validate_and_prepare_config(config):
    num_cols = config.get("num_cols")
    if not isinstance(num_cols, int) or num_cols <= 0:
        raise ValueError("num_cols must be a positive integer.")

    col_per_file = resolve_col_per_file(config.get("col_per_file"), num_cols)
    char_per_col = resolve_char_per_col(config.get("char_per_col"), num_cols)
    custom = parse_custom_field(config.get("custom", []))
    #outfile_dir = config.get("outfile_dir", "out")
    #num_output_split = config.get("num_output_split", 1)

    return {
        "num_cols": num_cols,
        "col_per_file": col_per_file,
        "char_per_col": char_per_col,
        "custom": custom,
        #"outfile_dir": outfile_dir,
        #"num_output_split": num_output_split,
    }


def split_key(s, n):
    """Split string s into n nearly equal parts"""
    k, m = divmod(len(s), n)
    return [s[i * k + min(i, m):(i + 1) * k + min(i + 1, m)] for i in range(n)]

# def build_column_dicts(input_dict, split):
#     col_dicts = [dict() for _ in range(split)]
#     counters = [itertools.count(1) for _ in range(split)]  # One counter per column

#     for key in input_dict:
#         parts = split_key(key, split)
#         for i, part in enumerate(parts):
#             if part not in col_dicts[i]:
#                 val = next(counters[i])
#                 col_dicts[i][part] = hex(val)[2:]
#                 # col_dicts[i][part] = str(next(counters[i]))  # Store as string for concat
#     return col_dicts

def build_column_dicts(input_dict, split):
    col_dicts = [dict() for _ in range(split)]
    counters = [itertools.count(1) for _ in range(split)]
    raw_ids_per_col = [dict() for _ in range(split)]  # Temporary raw int IDs

    # First pass: assign raw integer IDs per column
    for key in input_dict:
        parts = split_key(key, split)
        for i, part in enumerate(parts):
            if set(part) == {'?'}:
                continue
            if part not in raw_ids_per_col[i]:
                raw_ids_per_col[i][part] = next(counters[i])

    # Second pass: determine bit-width and convert to binary with padding
    for i in range(split):
        max_id = max(raw_ids_per_col[i].values())
        bit_width = math.ceil(math.log2(max_id + 1))  # +1 to ensure enough bits
        for part, int_id in raw_ids_per_col[i].items():
            bin_id = format(int_id, f'0{bit_width}b')  # padded binary string
            col_dicts[i][part] = bin_id

    return col_dicts


def build_final_dict(input_dict, col_dicts, split):
    final_dict = {}
    for key, value in input_dict.items():
        parts = split_key(key, split)
        mapped_ids = []
        for i in range(split):
            part = parts[i]

            if set(part) == {'?'}:
                first_val = next(iter(col_dicts[i].values()), 0)
                bit_width = len(first_val)
                mapped_ids.append('?'*bit_width)
            else:
                mapped_ids.append(col_dicts[i][part])


        #mapped_ids = [col_dicts[i][parts[i]] for i in range(split)]
        composite_key = ''.join(mapped_ids)
        final_dict[composite_key] = value
    return final_dict

def count_luts(n, k=6):
    #n is the number of inputs, k is the lut size
    # print(f"count luts called with: {n}")
    curr_n = n
    total_luts = 1
    
    if curr_n == 0:
        return 0

    while curr_n > k:
        curr_n = math.ceil(curr_n/k)
        total_luts += curr_n
    
    return total_luts

def extract_integer_before_quote(text: str) -> Optional[int]:
    """
    Extracts the integer that appears before a single quote in the given string.
    Example: "123'abc" -> 123
    """
    match = re.match(r"^(\d+)'[\w\d]+$", text)
    if match:
        return int(match.group(1))
    return None

def key_to_condition_v2(segments, signal_name="data_in"):
    """
    Convert a list of bitstring segments into a SystemVerilog condition expression.
    Skips segments that are entirely '?', but their bit positions are still counted.
    Example:
        ["01", "?", "100", "??", "11"]
        -> ((data_in[9:8] == 2'b01) && (data_in[6:4] == 3'b100) && (data_in[1:0] == 2'b11))
    """
    total_bits = sum(len(seg) for seg in segments)
    msb = total_bits - 1
    bit_cursor = msb
    conditions = []

    for seg in segments:
        width = len(seg)
        if all(ch == '?' for ch in seg):
            # skip segment, just advance position
            bit_cursor -= width
            continue

        high = bit_cursor
        low = bit_cursor - width + 1
        if width == 1:
            bit_range = f"{high}"
        else:
            bit_range = f"{high}:{low}"

        cond = f"({signal_name}[{bit_range}] == {width}'b{seg})"
        conditions.append(cond)

        bit_cursor -= width  # move to next segment's LSB

    return "(" + " && ".join(conditions) + ")"


def key_to_condition(bitstring, signal_name="data_in"):
    """
    Convert a bitstring like '0101??1010' into a SystemVerilog condition expression.
    """
    conditions = []
    msb = len(bitstring) - 1
    idx = 0
    while idx < len(bitstring):
        if bitstring[idx] == '?':
            idx += 1
            continue
        # find contiguous region of fixed bits
        start = idx
        while idx < len(bitstring) and bitstring[idx] in ('0', '1'):
            idx += 1
        end = idx - 1
        bit_range = f"{msb - start}:{msb - end}" if end != start else f"{msb - start}"
        segment_bits = bitstring[start:idx]
        conditions.append(f"{signal_name}[{bit_range}] == {len(segment_bits)}'b{segment_bits}")
    return " && ".join(conditions) if conditions else "1'b1"

def gen_assign_statements(condition_dict, var_prefix="", position_idx=""):
    """
    Generalized condition variable extractor.

    Args:
        condition_dict: dict {condition_expr: value}
            e.g., {"(data_in[9:6] == 4'b0101 && data_in[3:0] == 4'b1010)": "10'b001"}
        signal_name: base signal name to detect (default: "data_in")

    Returns:
        {
            "var_defs": dict {var_name: comparator_expr},
            "updated_conditions": dict {updated_condition: value}
        }
    """
    var_defs = {}     # var_name -> full_comparator
    updated_conditions = {}

    ii=0
    for cond, value in condition_dict.items():
        prefix = ""
        if var_prefix != "":
            prefix = var_prefix
            if position_idx != "":
                prefix += "_" + str(position_idx)
        
        var_name = prefix + "_row_" + str(ii)
        var_defs[var_name] = "(" + cond + ")"
        updated_conditions[var_name] = value
        ii = ii+1

    return {
        "var_defs": var_defs,
        "updated_conditions": updated_conditions
    }


def extract_condition_vars(condition_dict, signal_name="data_in"):


    # Matches any of:
    #   data_in[9:6] == 4'b0101
    #   data_in[12+:4] == 4'b1010
    #   data_in[20-:8] == 4'b0011
    comparator_pattern = re.compile(
        rf"({signal_name}\[\s*[\d+\-:]+\s*\]\s*==\s*\d*'[bBdDhHoO][0-9a-fA-FxXzZ_]+)"
    )
    
    

    var_defs = {}     # var_name -> full_comparator
    updated_conditions = {}

    for cond, value in condition_dict.items():
        new_cond = cond
        for match in comparator_pattern.finditer(cond):
            full_cmp = match.group(1)

            # Build a safe variable name from the comparator
            safe_name = (
                full_cmp
                .replace(signal_name, signal_name)
                .replace('[', '_')
                .replace(']', '')
                .replace(':', '_')
                .replace('+', 'p')
                .replace('-', 'm')
                .replace(' ', '')
                .replace("==", '_')
                .replace("'", "")
                .replace('b', '_')
            )

            # Keep only alphanumeric and underscores
            safe_name = re.sub(r'[^a-zA-Z0-9_]', '', safe_name)

            var_name = safe_name
            if var_name not in var_defs:
                var_defs[var_name] = full_cmp

            new_cond = new_cond.replace(full_cmp, var_name)

        updated_conditions[new_cond] = value

    return {
        "var_defs": var_defs,
        "updated_conditions": updated_conditions
    }

def build_condition_tree(segment_list, default_id=None):
    tree = {}
    if default_id is not None:
        tree['_default_'] = default_id
    for seg in segment_list:
        cond_str = seg['condition']
        seg_id = seg['segment_id']

        # Strip parentheses and split by &&
        parts = [p.strip(" ()") for p in cond_str.split('&&')]
        node = tree
        
        for p in parts[:-1]:
            if p not in node:
                node[p] = {}
            elif not isinstance(node[p], dict):
                # Convert existing leaf ID to a branch, storing the ID in None
                node[p] = {None: node[p]}
            node = node[p]
        
        last_part = parts[-1]
        if last_part in node:
            if isinstance(node[last_part], dict):
                # Store the ID at this level of the existing branch
                node[last_part][None] = seg_id
            else:
                node[last_part] = seg_id # Overwrite if exact duplicate
        else:
            node[last_part] = seg_id
            
    return tree
    
# def build_condition_tree(segment_list, default_id=None):
#     """
#     Convert a list of flat ANDed conditions like:
#         (a && b && c)
#         (a && b && d)
#     into a nested tree:
#         {'a': {'b': {'c': 0, 'd': 1}}}
#     """
#     tree = {}
#     for seg in segment_list:
#         cond_str = seg['condition']
#         seg_id = seg['segment_id']

#         # Strip parentheses and split by &&
#         parts = [p.strip(" ()") for p in cond_str.split('&&')]
#         node = tree
#         for p in parts[:-1]:
#             node = node.setdefault(p, {})
#         node[parts[-1]] = seg_id

#     if default_id is not None:
#         tree['_default_'] = default_id

#     return tree