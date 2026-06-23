from utils.utils import (
    jj_render, 
    get_build_rtl_parse_args, 
    load_graph_config,
    load_mask,
    validate_and_prepare_config,
    clog2,
    str_to_hex,
    split_key,
    build_column_dicts,
    build_final_dict,
    count_luts,
    extract_integer_before_quote,
    key_to_condition,
    extract_condition_vars,
    build_condition_tree,
    gen_assign_statements,
    key_to_condition_v2,
    refine_char_per_col,
    remap_col_per_file
)
import os
import re
from collections import defaultdict
from pathlib import Path
import random
import json

HEX_GROUP_REGEX = re.compile(r'[0-9A-Fa-f]+')

TB_RANDOM_SEED = 0x5A5A1234
_HEX_FILL_DIGITS = "0123456789abcdef"


def select_tb_lines(input_lines, custom_tokens, max_lines=None):
    """
    Pick lines from the mask file to drive in an auto-generated testbench.

    Each returned entry is a dict:
        {hex_orig, hex_filled, opcode, extraction_str, path_id}

    - hex_orig    : the literal/wildcard pattern straight from the mask file
                    (used to locate which nibbles are real comparison literals)
    - hex_filled  : hex_orig with every 'x'/'X' nibble replaced by a concrete,
                    deterministically-randomized hex digit (so the stimulus is
                    a realistic concrete packet -- no 4-state 'X' driven into
                    the DUT, and golden field values can be sliced out of it)
    - opcode      : golden path_id column from the mask file
    - path_id     : 1-based line index, matching build_extractor_rtl()'s
                    path_id = i + 1 and the mask's "7'd<N>" golden column

    Lines whose hex pattern contains a custom-token id are skipped entirely --
    there is no principled way to derive a golden value for a user-defined
    port/opcode/constant token here (see README "Tool Limitations").

    If max_lines is given, sub-selects an evenly-spaced sample across the
    distinct opcodes (in order of first appearance) so the chosen lines span
    different path "families" rather than near-duplicate adjacent lines.
    """
    rng = random.Random(TB_RANDOM_SEED)

    parsed = []
    for i, line in enumerate(input_lines):
        if not line.strip():
            continue
        parts = line.strip().split()
        if len(parts) < 2:
            continue

        hex_orig, opcode = parts[0], parts[1]
        extraction_str = parts[2] if len(parts) > 2 else ""

        if any(ctoken["id"] in hex_orig for ctoken in custom_tokens):
            continue

        parsed.append((i, hex_orig, opcode, extraction_str))

    # The comparison RTL decomposes each column into literal-value comparators
    # at whatever granularity (nibble, byte, or wider) keeps paths
    # distinguishable -- e.g. comparison_logic_*.sv mixes `data_in[47-:4] ==
    # 4'h6` (nibble-level) and `data_in[47-:8] == 8'h2b` (byte-level) for
    # overlapping byte positions -- and reports a "no known literal matched"
    # code for any other value. If a randomly filled don't-care nibble/byte
    # happens to collide with a literal value some *other* path uses at that
    # same position, the comparator reports that other path's code instead of
    # "don't care" and the lookup fails to match (golden mismatch). To avoid
    # this, collect the literal values observed at each nibble position and
    # each byte position and steer the random fill away from all of them.
    reserved_by_nibble_pos = defaultdict(set)
    reserved_by_byte_pos = defaultdict(set)
    for _, hex_orig, _, _ in parsed:
        for pos, ch in enumerate(hex_orig):
            if ch.lower() != 'x':
                reserved_by_nibble_pos[pos].add(ch.lower())
        for bp in range(len(hex_orig) // 2):
            hi, lo = hex_orig[2 * bp], hex_orig[2 * bp + 1]
            if hi.lower() != 'x' and lo.lower() != 'x':
                reserved_by_byte_pos[bp].add((hi.lower(), lo.lower()))

    def pick_nibble(pos):
        reserved = reserved_by_nibble_pos.get(pos, ())
        choices = [d for d in _HEX_FILL_DIGITS if d not in reserved] or list(_HEX_FILL_DIGITS)
        return rng.choice(choices)

    def fill_hex(hex_orig):
        chars = list(hex_orig)
        n = len(chars)
        i = 0
        while i < n:
            bp = i // 2
            if i + 1 < n and chars[i].lower() == 'x' and chars[i + 1].lower() == 'x':
                byte_reserved = reserved_by_byte_pos.get(bp, ())
                for _ in range(64):
                    hi, lo = pick_nibble(i), pick_nibble(i + 1)
                    if (hi, lo) not in byte_reserved:
                        break
                chars[i], chars[i + 1] = hi, lo
                i += 2
            else:
                if chars[i].lower() == 'x':
                    chars[i] = pick_nibble(i)
                i += 1
        return "".join(chars)

    candidates = []
    for i, hex_orig, opcode, extraction_str in parsed:
        hex_filled = fill_hex(hex_orig)

        candidates.append({
            "hex_orig": hex_orig,
            "hex_filled": hex_filled,
            "opcode": opcode,
            "extraction_str": extraction_str,
            "path_id": i + 1,
        })

    if max_lines is None or len(candidates) <= max_lines:
        return candidates

    # Sample evenly across distinct opcodes (in order of first appearance) so
    # the selected lines land in different path families.
    seen_opcodes = []
    first_by_opcode = {}
    for c in candidates:
        if c["opcode"] not in first_by_opcode:
            first_by_opcode[c["opcode"]] = c
            seen_opcodes.append(c["opcode"])

    step = max(1, len(seen_opcodes) // max_lines)
    sampled_opcodes = seen_opcodes[::step][:max_lines]
    return [first_by_opcode[op] for op in sampled_opcodes]

def extract_segments_and_tokens(input_lines, char_per_col, num_cols, character_ids, custom_tokens, debug):
    """
    Args:
        input_lines: list of strings like '<hexstring> <id> [extractions]'
        ...
    """
    segment_id_map = defaultdict(dict)
    segment_id_counter = [1] * num_cols
    segment_token_details = defaultdict(list)  # group by col_idx

    for row_num, line in enumerate(input_lines):
        if not line.strip():
            continue
        parts = line.strip().split()
        if not parts:
            continue
        if len(parts) < 2:
            raise ValueError(f"mask file should have at least two parts <mask><space><id>")

        # We only care about parts[0] (HEX) here. parts[1] is ID, parts[2] is extraction (ignored here)
        hex_part = parts[0]

        for col_idx, col_width in enumerate(char_per_col):
            col_start_global = character_ids[col_idx]
            col_end_global = col_start_global + col_width
            segment = hex_part[col_start_global:col_end_global]

            if (debug):
                print(f"SEGMENT: {segment}")

            # Extract hex tokens first to check if segment contains any hex
            tokens = []
            for match in HEX_GROUP_REGEX.finditer(segment):
                tokens.append({
                    'token': match.group(),
                    'start': col_start_global + match.start(),
                    'end': col_start_global + match.end(),
                    'ctoken_port': "",
                    'ctoken_type': "",
                    'ctoken_cnt': 0,
                })

            # Find all custom tokens occurrences (dictionary-based schema)
            for ctoken in custom_tokens:
                ctoken_id = ctoken["id"]
                ctoken_type = ctoken["type"]
                ctoken_opcode = ctoken["opcode"]

                start = 0
                while True:
                    idx = segment.find(ctoken_id, start)
                    if idx == -1:
                        break

                    # Handle port-based entries
                    if ctoken_type == "port":
                        port_name = ctoken["prefix"]
                        num_ports = ctoken.get("num_ports", 0)
                        tokens.append({
                            "token": ctoken_id,
                            "start": idx,
                            "end": idx + len(ctoken_id),
                            "ctoken_port": port_name,
                            "ctoken_type": ctoken_opcode,
                            "ctoken_cnt": num_ports
                        })

                    # Handle const-based entries
                    elif ctoken_type == "const":
                        tokens.append({
                            "token": ctoken_id,
                            "start": idx,
                            "end": idx + len(ctoken_id),
                            "ctoken_port": "",  # no port name
                            "ctoken_type": ctoken_opcode,
                            "ctoken_cnt": len(ctoken["values"]),
                            "ctoken_values": ctoken["values"],
                        })

                    start = idx + 1  # move past this match to find next


            # Skip segment if no hex tokens found (all non-hex)
            if not tokens:
                continue
                
            # If segment already seen, skip processing to avoid duplicates
            if segment in segment_id_map[col_idx]:
                continue

            # Assign unique ID for new segment
            segment_id = segment_id_counter[col_idx]
            segment_id_map[col_idx][segment] = segment_id
            segment_id_counter[col_idx] += 1

            # Store details grouped by column
            segment_token_details[col_idx].append({
                'segment': segment,
                'segment_id': segment_id,
                'tokens': tokens
            })

    return segment_id_map, segment_token_details, segment_id_counter

def build_lookup_rtl_tree (input_lines, char_per_col, num_cols, character_ids,
                     segment_token_details, segment_id_counter,
                     num_output_split, outfile_dir, retime, debug):
    
    def insert_nested(d, seg_ids, value):
        if not seg_ids:
            d["__value__"] = value
            return
        head, *tail = seg_ids
        if head not in d:
            d[head] = {}
        insert_nested(d[head], tail, value)

    segmented_id_rows = []

    for line in input_lines:
        if not line.strip():
            continue
        parts = line.strip().split()
        if len(parts) < 2:
            raise ValueError("Each line must have at least <hexstring> <opcode>")

        hex_part, opcode = parts[0], parts[1]
        row_seg_ids = []

        for col_idx, col_width in enumerate(char_per_col):
            col_start_global = character_ids[col_idx]
            col_end_global = col_start_global + col_width
            segment = hex_part[col_start_global:col_end_global]

            if col_idx in segment_token_details:
                bit_width = clog2(segment_id_counter[col_idx])
                for seg in segment_token_details[col_idx]:
                    if seg["segment"] == segment:
                        seg_id = f"{seg['segment_id']:0{bit_width}b}"
                        break
                else:
                    seg_id = "?" * bit_width
                row_seg_ids.append(seg_id)
        
        segmented_id_rows.append((row_seg_ids, opcode))

    # Build nested if-else tree
    nested_dict = {}
    for seg_ids, opcode in segmented_id_rows:
        clean_ids = []
        for s in seg_ids:
            if '?' in s:
                clean_ids.append(f"__skip__{len(s)}")
            else:
                clean_ids.append(s)
        insert_nested(nested_dict, clean_ids, opcode)

    din_bits = sum(clog2(segment_id_counter[idx]) for idx in segment_token_details.keys())
    first_valid_opcode = next((v for _, v in segmented_id_rows if v), "0")
    dout_bits = extract_integer_before_quote(str(first_valid_opcode))

    module_name = "lookup_logic"

    context = {
        'din_bits': din_bits,
        'dout_bits': dout_bits,
        'nested_tree': nested_dict,
        'module': module_name,
    }

    jj_render("lookup_rtl_nested_ifelse_tmpl.sv", module_name, context, outfile_dir)

    return [din_bits], [dout_bits]

def build_lookup_rtl (input_lines, char_per_col, num_cols, character_ids, segment_token_details, segment_id_counter, num_output_split, outfile_dir, retime, unique, debug):
    row_seg_id_dict = {}
    row_seg_id_dict_cond = {}
    din_outb_list = []
    dout_outb_list = []
    segmented_id_rows = []
    
    for row_num, line in enumerate(input_lines):
        if not line.strip():
            continue
        parts = line.strip().split()
        if len(parts) < 2:
            raise ValueError(f"mask file should have at least two parts <mask><space><id>")

        hex_part = parts[0]
        hex_value = parts[1]
        row_seg_ids = []

        for col_idx, col_width in enumerate(char_per_col):
            col_start_global = character_ids[col_idx]
            col_end_global = col_start_global + col_width
            segment = hex_part[col_start_global:col_end_global]

            if col_idx in segment_token_details:
                bit_width = clog2(segment_id_counter[col_idx])
                for seg in segment_token_details[col_idx]:
                    if seg["segment"] == segment:
                        seg_id = f"{seg['segment_id']:0{bit_width}b}"
                        break
                else:
                    seg_id = "?" * bit_width
                row_seg_ids.append(seg_id)
        
        final_seg_id = ''.join(row_seg_ids)
        segmented_id_rows.append((row_seg_ids, hex_value))
        row_seg_id_dict[final_seg_id] = str(hex_value)

    if debug:
        print(f"Unique IDs and the corresponding values: {row_seg_id_dict}")
    
    if num_output_split == 1:
        first_key = next(iter(row_seg_id_dict))
        first_value = row_seg_id_dict[first_key]

        din_bits = len(first_key)
        dout_bits = extract_integer_before_quote(str(first_value))
        module_name = "lookup_logic"

        add_reg = 0
        if retime:
            add_reg = 1

        context = {
            'din_bits' : din_bits,
            'dout_bits' : dout_bits,
            'data' : row_seg_id_dict,
            'var_defs': {},
            'allow_dontcare': 1,
            'add_reg': add_reg,
            'output_type' : "x",
            'module' : module_name
        }

        if unique:
            row_seg_id_dict_cond = {key_to_condition_v2(k): v for k, v in segmented_id_rows}
            cond_info = gen_assign_statements (row_seg_id_dict_cond, var_prefix="lookup")
            context['data'] = cond_info['updated_conditions']
            context['var_defs'] = cond_info['var_defs']
            jj_render("lookup_rtl_tmpl_unique.sv", module_name, context, outfile_dir)
        else:
            jj_render("lookup_rtl_tmpl.sv", module_name, context, outfile_dir)

        din_outb_list.append(din_bits)
        dout_outb_list.append(dout_bits)
    else:
        # Multi-file split logic remains same...
        col_dicts = build_column_dicts(row_seg_id_dict, num_output_split)
        final_dict = build_final_dict(row_seg_id_dict, col_dicts, num_output_split)

        for i, d in enumerate(col_dicts):
            first_key = next(iter(d))
            first_value = d[first_key]
            din_bits = len(first_key)
            dout_bits = len(first_value)
            module_name = "lookup_logic_partial_" + str(i)
            context = {
                'din_bits' : din_bits,
                'dout_bits' : dout_bits,
                'data' : d,
                'allow_dontcare': 1,
                'output_type' : "b",
                'module' : module_name
            }
            din_outb_list.append(din_bits)
            dout_outb_list.append(dout_bits)
            jj_render("lookup_rtl_tmpl.sv", module_name, context, outfile_dir)
        
        first_key = next(iter(final_dict))
        first_value = final_dict[first_key]
        din_bits = len(first_key)
        dout_bits = extract_integer_before_quote(str(first_value))
        din_outb_list.append(din_bits)
        dout_outb_list.append(dout_bits)
        module_name = "lookup_logic_final"
        context = {
            'din_bits' : din_bits,
            'dout_bits' : dout_bits,
            'data' : final_dict,
            'allow_dontcare': 0,
            'output_type' : "x",
            'module' : module_name
        }
        jj_render("lookup_rtl_tmpl.sv", module_name, context, outfile_dir)

    return din_outb_list, dout_outb_list

def build_top_rtl(
    active_modules,
    lookup_din_outb_list,
    lookup_dout_outb_list,
    custom_inputs,
    outfile_dir,
    num_cols
):
    lookup_needed = 1
    #if num_cols == 1:
    #    lookup_needed = 0

    module_name = "hex_to_rtl_top"
    context = {
        'active_modules': active_modules,
        'lookup_din_outb_list': lookup_din_outb_list,
        'lookup_dout_outb_list': lookup_dout_outb_list,
        'lookup_needed': lookup_needed,
        'custom_inputs': custom_inputs,
        'module' : module_name
    }

    jj_render("hex_to_rtl_top_tmpl.sv", module_name, context, outfile_dir)

    # Emit the (static, parameterized) AXI skid buffer module. It is instantiated
    # inside hex_to_rtl_top when SKID_EN=1, and by parser_top when it owns the skid.
    jj_render("axi_skid.sv", "axi_skid", {}, outfile_dir)

def build_toptb_rtl(
    input_lines,
    active_modules,
    lookup_dout_outb_list,
    custom_inputs,
    custom_tokens,
    outfile_dir
):
    comp_din_bits_list = [mod['din_bits'] for mod in active_modules]

    selected = select_tb_lines(input_lines, custom_tokens)
    stimulus_list = [s["hex_filled"] for s in selected]
    golden_list = [s["opcode"] for s in selected]

    stimulus_width = len(stimulus_list[0])*4

    num_chunks = stimulus_width // comp_din_bits_list[0]

    module_name = "hex_to_rtl_top_tb"
    context = {
        'comp_din_bits_list': comp_din_bits_list,
        'lookup_dout_outb_list': lookup_dout_outb_list,
        'stimulus_list' : stimulus_list,
        'golden_list': golden_list,
        'stimulus_width': stimulus_width,
        'num_chunks': num_chunks,
        'custom_inputs': custom_inputs,
        'module' : module_name
    }

    jj_render("hex_to_rtl_toptb_tmpl.sv", module_name, context, outfile_dir)

def build_comparison_rtl (segment_token_details, character_ids, col_per_file, segment_id_counter, tree, retime, unique, outfile_dir):
    start_col_id = 0
    active_modules = []
    combined_custom_inputs_used = set()
    for file_idx, num_col_this_file in enumerate(col_per_file):
        module_name = "comparison_logic_" + str(file_idx)

        end_col_id = start_col_id + num_col_this_file
        num_chars_this_file = character_ids[end_col_id] - character_ids[start_col_id]

        din_bits = num_chars_this_file * 4 
        dout_bits = 0

        all_positions = []
        custom_inputs_used = set()
        token_offset = character_ids[start_col_id]
        
        for position in sorted(segment_token_details.keys()):
            segment_list = []
            max_segment_id = segment_id_counter[position]

            if (position >= start_col_id) and (position < end_col_id):
                for seg in segment_token_details[position]:
                    comparisons = []
                    for token in seg['tokens']:
                        width = (token['end'] - token['start']) * 4
                        start_bit = din_bits - 1 - ((token['start'] - token_offset) * 4)
                        ctoken_type = token.get('ctoken_type')

                        if ctoken_type == "EQOR":
                            if token.get('ctoken_port'):
                                ctoken_comparisons = [
                                    f"(data_in[{start_bit}-:{width}] == {token['ctoken_port']}_{ii})"
                                    for ii in range(int(token.get('ctoken_cnt', 0)))
                                ]
                                for ii in range(int(token.get('ctoken_cnt', 0))):
                                    custom_inputs_used.add((f"{token['ctoken_port']}_{ii}", width))
                            elif token.get('ctoken_values'):
                                ctoken_comparisons = [
                                    f"(data_in[{start_bit}-:{width}] == {width}'h{val})"
                                    for val in token['ctoken_values']
                                ]
                            comparisons.append(f"({' || '.join(ctoken_comparisons)})")

                        elif ctoken_type == "NE":
                            if token.get('ctoken_port'):
                                comparisons.append(f"(data_in[{start_bit}-:{width}] != {token['ctoken_port']})")
                                custom_inputs_used.add((token['ctoken_port'], width))
                            elif token.get('ctoken_values'):
                                comparisons.append(
                                    f"(data_in[{start_bit}-:{width}] != {width}'h{token['ctoken_values'][0]})"
                                )
                        elif ctoken_type == "RANGE":
                            if token.get('ctoken_port'):
                                comparisons.append(
                                    f"(data_in[{start_bit}-:{width}] >= {token['ctoken_port']}_0) && "
                                    f"(data_in[{start_bit}-:{width}] < {token['ctoken_port']}_1)"
                                )
                                custom_inputs_used.add((f"{token['ctoken_port']}_0", width))
                                custom_inputs_used.add((f"{token['ctoken_port']}_1", width))
                            elif token.get('ctoken_values'):
                                vals = token['ctoken_values'][:2]
                                comparisons.append(
                                    f"(data_in[{start_bit}-:{width}] >= {width}'h{vals[0]}) && "
                                    f"(data_in[{start_bit}-:{width}] < {width}'h{vals[1]})"
                                )
                        else: 
                            hex_value = str_to_hex(token['token'])
                            comparisons.append(f"(data_in[{start_bit}-:{width}] == {hex_value})")

                    full_condition = f"({' && '.join(comparisons)})"
                    seg_id = seg['segment_id']
                    segment_list.append({
                        'condition': full_condition,
                        'segment_id': seg_id
                    })

                output_width = clog2(max_segment_id)
                position_entry = {
                    'conditions': segment_list,
                    'var_defs' : {},
                    'nested_tree' : {},
                    'output_width': output_width,
                    'position_index': position,
                }

                if tree:
                    nested_tree = build_condition_tree(segment_list, default_id=0)
                    position_entry['nested_tree'] = nested_tree
                elif unique:  
                    segment_dict = {seg['condition']: seg['segment_id'] for seg in segment_list}
                    cond_info = gen_assign_statements (segment_dict, var_prefix="comparison", position_idx=position)
                    new_segment_list = [{'condition': cond, 'segment_id' : seg_id} for cond, seg_id in cond_info['updated_conditions'].items()]

                    position_entry['conditions'] = new_segment_list
                    position_entry['var_defs'] = cond_info['var_defs']

                all_positions.append(position_entry)
                dout_bits += output_width
        
        if not all_positions:
            start_col_id = end_col_id
            continue

        custom_inputs = [{"name": name, "width": width} for (name, width) in custom_inputs_used]

        add_reg = 0
        if retime:
            add_reg = 1

        context = {
            'din_bits' : din_bits,
            'dout_bits' : dout_bits,
            'all_positions' : all_positions,
            'module' : module_name,
            'custom_in': custom_inputs,
            'add_reg' : add_reg
        }
        active_modules.append({
            "id": file_idx,
            "din_bits": din_bits,
            "dout_bits": dout_bits
        })

        if tree:
            template_name = "comparison_rtl_tmpl_tree.sv"
        elif unique:
            template_name = "comparison_rtl_tmpl_unique.sv"
        else:
            template_name = "comparison_rtl_tmpl.sv"

        jj_render(template_name, module_name, context, outfile_dir)
        
        start_col_id = end_col_id
        combined_custom_inputs_used.update(custom_inputs_used)
    
    combined_custom_inputs = [{"name": name, "width": width} for (name, width) in combined_custom_inputs_used]

    return active_modules, combined_custom_inputs

def build_extractor_rtl(input_lines, outfile_dir, max_dbits):
    """
    Parses the 3rd column of input_lines and generates the extractor RTL.
    Returns the list of fields (ports) if extraction is present, otherwise empty.
    """
    field_positions = {}
    max_bus_position = 0
    paths_with_extraction = 0
    total_paths = 0

    for i, line in enumerate(input_lines):
        if not line.strip():
            continue
        total_paths += 1
        parts = line.strip().split()
        
        # Check if 3rd column exists
        if len(parts) < 3:
            continue
        
        extraction_str = parts[2]
        if not extraction_str:
            continue
            
        path_id = i + 1
        paths_with_extraction += 1
        
        # Parse tuple string: field:msb:lsb;field:msb:lsb
        tuples = extraction_str.split(';')
        for t in tuples:
            if not t: continue
            try:
                f_id, msb, lsb = t.split(':')
                msb = int(msb)
                lsb = int(lsb)
            except ValueError:
                print(f"Warning: Malformed extraction tuple '{t}' on line {i+1}")
                continue

            if msb > max_bus_position: max_bus_position = msb

            if f_id not in field_positions:
                field_positions[f_id] = {}
            
            pos_key = (msb, lsb)
            if pos_key not in field_positions[f_id]:
                field_positions[f_id][pos_key] = []
            
            field_positions[f_id][pos_key].append(path_id)

    if paths_with_extraction == 0:
        return []

    # Prepare data for template
    fields = []
    for field_name, positions_dict in field_positions.items():
        # positions_dict: {(msb, lsb): [path_ids...]}
        position_cases = []
        max_field_width = 0
        
        for (msb, lsb), path_ids in positions_dict.items():
            length = msb - lsb + 1
            if length > max_field_width: max_field_width = length
            
            position_cases.append({
                "path_ids": sorted(path_ids),
                "msb": msb,
                "lsb": lsb,
                "length": length
            })
        
        fields.append({
            "name": field_name,
            "width": max_field_width,
            "positions": sorted(position_cases, key=lambda x: x['msb'], reverse=True)
        })

    fields.sort(key=lambda x: x["name"])
    
    # Render Template
    # We assume bus_width is determined by max_bus_position or we could pass it in.
    # For now, let's infer strictly from max_bus_position found (rounded up to nearest byte/nibble if preferred, 
    # but exact is fine). Note: Template uses bus_width-1.
    print(f"DEBUG {max_dbits}")
    bus_width = max_dbits
    counter_width = clog2(total_paths)
    base_name = "mask" # generic name, usually overridden by caller context if needed, but here we fix it or derive
    
    # We use a fixed module name suffix based on logic in build_mask previously
    module_name = "mask_extractor" 
    
    context = {
        "module_name": module_name,
        "bus_width": bus_width,
        "counter_width": counter_width,
        "fields": fields
    }
    
    jj_render("extractor_template.sv", module_name, context, outfile_dir)
    
    return fields


def build_parser_top(config, extractor_fields, outfile_dir):
    """
    Generates parser_top.sv
    """
    # Retrieve injected config parameters
    data_width = config.get("data_width")
    max_width = config.get("max_width")
    
    if data_width is None or max_width is None:
        print("Warning: data_width or max_width missing in config. parser_top generation might fail or be incorrect.")
        return

    # path_id_width is needed. 
    # We can infer it from the number of lines in mask? 
    # Or cleaner: build_rtl_countluts should return it or we re-calculate.
    # We will recalculate based on number of lines in mask is safe, 
    # OR we can assume build_comparison logic found it.
    # Let's read the mask again? No, we have input_lines in main scope.
    # We will pass path_id_width into this function from main.
    pass # logic moved to main to have access to path_id_width


def build_parser_toptb_rtl(input_lines, custom_tokens, extractor_fields,
                           data_width, max_width, path_id_width, outfile_dir):
    """
    Auto-generates a self-checking testbench for parser_top.

    Picks a handful of representative, token-free mask lines (see
    select_tb_lines), drives each as a real flit-streamed packet -- sized to
    the minimum number of flits that actually contains every comparison
    literal AND every extracted field for that path -- and checks the
    resulting field_*_out values against golden values sliced directly out of
    the same (don't-care-filled) stimulus using the mask's "field:msb:lsb"
    bit-position metadata. No semantic knowledge of the packet format is
    needed: the mask hex string already tells us where the literal/comparison
    nibbles and the extracted-field nibbles are.
    """
    nibbles_per_flit = data_width // 4
    num_pipes = max_width // data_width

    selected = select_tb_lines(input_lines, custom_tokens, max_lines=4)

    packets = []
    for sel in selected:
        hex_orig = sel["hex_orig"]
        hex_filled = sel["hex_filled"]

        # Last literal (non-wildcard) nibble -- defines how far the
        # comparison logic needs to see into the packet.
        h_cmp = -1
        for idx, ch in enumerate(hex_orig):
            if ch.lower() != 'x':
                h_cmp = idx

        # A line's extraction string can list the same field at several
        # candidate bit ranges (e.g. "prot:607:600;...;prot:335:328") --
        # build_extractor_rtl() renders these as an if/else-if chain ordered
        # by descending msb, so the highest-msb candidate always wins for any
        # path that lists it. Replicate that selection here so the golden
        # value matches what mask_extractor will actually drive.
        field_tuples = defaultdict(list)
        for tup in sel["extraction_str"].split(';'):
            if not tup:
                continue
            try:
                f_name, msb_s, lsb_s = tup.split(':')
                field_tuples[f_name].append((int(msb_s), int(lsb_s)))
            except ValueError:
                continue

        h_ext = -1
        field_golden_by_name = {}
        for f_name, tuples in field_tuples.items():
            msb, lsb = max(tuples, key=lambda t: t[0])
            nib_start = (max_width - 1 - msb) // 4
            nib_end = (max_width - 1 - lsb) // 4
            h_ext = max(h_ext, nib_end)
            field_golden_by_name[f_name] = hex_filled[nib_start:nib_end + 1]

        h = max(h_cmp, h_ext, 0)
        n_flits = min(h // nibbles_per_flit + 1, num_pipes)

        flit_values = [
            hex_filled[i * nibbles_per_flit:(i + 1) * nibbles_per_flit]
            for i in range(n_flits)
        ]

        packets.append({
            "tag": f"path_id={sel['path_id']} (opcode {sel['opcode']})",
            "n_flits": n_flits,
            "flits": flit_values,
            "field_golden_by_name": field_golden_by_name,
        })

    # Per-field, ordered (one golden value per packet, in driving order).
    # A path that doesn't define a given field leaves mask_extractor's
    # case-default in effect, which drives the field to zero.
    field_goldens = {
        field["name"]: [
            pkt["field_golden_by_name"].get(field["name"], "0" * (field["width"] // 4 + 1))
            for pkt in packets
        ]
        for field in extractor_fields
    }

    module_name = "parser_top_tb"
    context = {
        "module": module_name,
        "data_width": data_width,
        "num_pipes": num_pipes,
        "fields": extractor_fields,
        "packets": packets,
        "field_goldens": field_goldens,
    }

    jj_render("parser_top_tb_tmpl.sv", module_name, context, outfile_dir)

def extract_design_features(config, input_lines):
    """
    Compute structural features from mask lines + config without generating any RTL.
    All values are available before the first Jinja2 render — same data the RTL
    generation functions consume.  Returns a flat dict ready for ML/regression use.
    """
    char_per_col = config["char_per_col"]
    num_cols     = config["num_cols"]
    custom_tokens = config["custom"]
    data_width   = config.get("data_width")
    max_width    = config.get("max_width")

    character_ids = {}
    current_index = 0
    for idx, width in enumerate(char_per_col):
        character_ids[idx] = current_index
        current_index += width
    character_ids[len(char_per_col)] = current_index

    _, segment_token_details, segment_id_counter = extract_segments_and_tokens(
        input_lines, char_per_col, num_cols, character_ids, custom_tokens, debug=False
    )

    active_cols = sorted(segment_token_details.keys())

    din_bits = sum(clog2(segment_id_counter[col]) for col in active_cols)

    total_segments        = 0
    total_tokens          = 0
    total_comparison_bits = 0
    max_tokens_per_seg    = 0
    for col in active_cols:
        for seg in segment_token_details[col]:
            total_segments += 1
            n_tok = len(seg["tokens"])
            total_tokens   += n_tok
            max_tokens_per_seg = max(max_tokens_per_seg, n_tok)
            for token in seg["tokens"]:
                total_comparison_bits += (token["end"] - token["start"]) * 4

    num_paths = sum(1 for l in input_lines if l.strip())
    dout_bits = clog2(num_paths)
    num_pipes = (max_width // data_width) if (data_width and max_width) else None

    return {
        "num_active_cols":        len(active_cols),
        "num_cols":               num_cols,
        "din_bits":               din_bits,
        "dout_bits":              dout_bits,
        "num_paths":              num_paths,
        "total_unique_segments":  total_segments,
        "total_comparison_bits":  total_comparison_bits,
        "avg_tokens_per_segment": total_tokens / total_segments if total_segments else 0,
        "max_tokens_per_segment": max_tokens_per_seg,
        "data_width":             data_width,
        "max_width":              max_width,
        "num_pipes":              num_pipes,
    }


def build_rtl_countluts(cfgfilename, mask, countluts, rtl, tree1, tree2, retime, unique1, unique2, outfile_dir, debug):
    raw_config = load_graph_config(cfgfilename)
    config = validate_and_prepare_config(raw_config)
    
    # Manually merge raw config keys that validate_and_prepare might have skipped but we need
    if "data_width" in raw_config: config["data_width"] = raw_config["data_width"]
    if "max_width" in raw_config: config["max_width"] = raw_config["max_width"]

    input_lines = load_mask(mask)
        
    if debug:
        print("======================================")
        print("User defined config:")
        print(config)
        print("======================================")

    char_per_col = config["char_per_col"]
    num_cols = config["num_cols"]
    custom_tokens = config["custom"]
    col_per_file = config["col_per_file"]
    num_output_split = 1

    # Default flow: refine user-specified columns to eliminate false
    # positional overlaps (literal hex runs vs. don't-care regions landing
    # at misaligned offsets within the same column) before any slicing --
    # see the column-overlap analysis. Splits column boundaries only where
    # actually needed; untroubled columns keep their original width.
    char_per_col, col_group_sizes = refine_char_per_col(input_lines, char_per_col)
    num_cols = len(char_per_col)
    col_per_file = remap_col_per_file(col_per_file, col_group_sizes)

    if debug:
        print(f"Refined char_per_col ({num_cols} cols): {char_per_col}")
        print(f"Refined col_per_file: {col_per_file}")

    character_ids = {}
    current_index = 0
    for idx, width in enumerate(char_per_col):
        character_ids[idx] = current_index
        current_index += width
    character_ids[len(char_per_col)] = current_index 

    segment_id_map, segment_token_details, segment_id_counter = extract_segments_and_tokens(input_lines, char_per_col, num_cols, character_ids, custom_tokens, debug)
    
    if debug:
        print(segment_token_details)
    
    if rtl:
        # 1. Build Comparison Logic
        active_modules, combined_custom_inputs = build_comparison_rtl(segment_token_details, character_ids, col_per_file, segment_id_counter, tree1, retime, unique1, outfile_dir)
        
        # 2. Build Lookup Logic
        if tree2:
            lookup_din_outb_list, lookup_dout_outb_list = build_lookup_rtl_tree(input_lines, char_per_col, num_cols, character_ids, segment_token_details, segment_id_counter, num_output_split, outfile_dir, retime, debug)
        else:
            lookup_din_outb_list, lookup_dout_outb_list = build_lookup_rtl(input_lines, char_per_col, num_cols, character_ids, segment_token_details, segment_id_counter, num_output_split, outfile_dir, retime, unique2, debug)
        
        # 3. Build hex_to_rtl_top
        build_top_rtl (active_modules, lookup_din_outb_list, lookup_dout_outb_list, combined_custom_inputs, outfile_dir, num_cols)
        
        # 4. Build Testbench
        build_toptb_rtl (input_lines, active_modules, lookup_dout_outb_list, combined_custom_inputs, custom_tokens, outfile_dir)

        # 5. Build Extractor (if 3rd column exists)
        max_dbits = sum(char_per_col) * 4
        extractor_fields = build_extractor_rtl(input_lines, outfile_dir, max_dbits)

        # 6. Build parser_top
        # Calculate path_id_width based on number of valid mask lines
        valid_lines = [l for l in input_lines if l.strip()]
        path_id_width = clog2(len(valid_lines))
        
        # Generate parser_top
        module_name = "parser_top"
        dw = config.get("data_width")
        mw = config.get("max_width")
        
        if dw and mw:
            num_pipes = mw // dw
            context = {
                "module": module_name,
                "top_in_width": dw,
                "path_id_width": path_id_width,
                "fields": extractor_fields, # Empty list if no extraction
                "extractor_inwidth": mw,
                "num_pipes": num_pipes
            }
            jj_render("parser_top_rtl_tmpl.sv", module_name, context, outfile_dir)

            # 7. Build parser_top testbench
            build_parser_toptb_rtl(input_lines, custom_tokens, extractor_fields,
                                   dw, mw, path_id_width, outfile_dir)
        else:
            print("Skipping parser_top generation: data_width or max_width missing in config.")

    if countluts:
        comparison_val = estimate_lut_count_comparison (segment_token_details, segment_id_counter, debug)
        lookup_val = estimate_lut_count_lookup (input_lines, segment_token_details, segment_id_counter, debug)
        print(f"LUT estimated: {comparison_val + lookup_val}")
        return (comparison_val + lookup_val)

    return None

def main():
    args = get_build_rtl_parse_args()
    countluts = False 
    retime = False 

    if args.config_dir:
        folder = Path(args.config_dir).resolve()
        if not folder.is_dir():
            raise ValueError(f"{folder} is not a directory")

        for path in folder.glob("*.json"):
            abs_path = path.resolve()
            print(f"Processing {abs_path}")
            build_rtl_countluts(abs_path, args.mask, countluts, args.rtl, args.tree1, args.tree2, retime, args.unique1, args.unique2, args.outdir, args.debug)
    
    elif args.config:
        abs_path = Path(args.config).resolve()
        if not abs_path.is_file():
            raise ValueError(f"{abs_path} is not a file")

        build_rtl_countluts(abs_path, args.mask, countluts, args.rtl, args.tree1, args.tree2, retime, args.unique1, args.unique2, args.outdir, args.debug)

if __name__ == "__main__":
    main()
