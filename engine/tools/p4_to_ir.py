import json
import math
import re
import os
import subprocess

from utils.utils import (
    get_p4_to_ir_args,
    clog2,
    adjust_width
)


def generate_packed_hex(p4_file, width_str, output_folder, debug):

    p4_path = os.path.abspath(p4_file)
    # Check file exists
    if not os.path.isfile(p4_path):
        raise FileNotFoundError(f"P4 file not found: {p4_file}")

    p4_basename = os.path.splitext(os.path.basename(p4_path))[0]

    # Build command
    cmd = [
        "p4c",
        "--target", "bmv2",
        "--arch", "v1model",
        "-o", output_folder,
        p4_path
    ]

    if debug:
        print(f"Building P4 to JSON {p4_file}")
    # Run P4 command
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"p4c failed with exit code {e.returncode}") from e

    if debug:
        print(f"P4 compiled successfully..")

    char_width = int(width_str)

    json_file = os.path.join(output_folder, f"{p4_basename}.json")

    with open(json_file, 'r') as f:
        data = json.load(f)

    if debug:
        print(f"JSON loaded {json_file}")

    # --- 1. MAPPINGS ---
    header_inst_to_type = {h['name']: h['header_type'] for h in data['headers']}
    header_type_to_fields = {}
    for ht in data['header_types']:
        # [Name, Width]
        fields = [(f[0], f[1]) for f in ht['fields']]
        header_type_to_fields[ht['name']] = fields

    parser = data['parsers'][0]
    states_dict = {state['name']: state for state in parser['parse_states']}

    # --- 2. MATH & PARSING HELPERS ---
    def evaluate_expression(node, context_matches):
        """Recursively solves a JSON Math AST."""
        if node is None: return 0
        if node.get("type") == "expression": return evaluate_expression(node["value"], context_matches)
        if node.get("type") == "hexstr": return int(node["value"], 16)
        if node.get("type") == "decimal": return int(node["value"])
        if node.get("type") == "field":
            field_name = node["value"][1]
            if field_name in context_matches: return int(context_matches[field_name], 16)
            else: return 0 # Default if unknown
        if "op" in node:
            op = node["op"]
            l = evaluate_expression(node.get("left"), context_matches)
            r = evaluate_expression(node.get("right"), context_matches)
            if op == "+": return l + r
            if op == "-": return l - r
            if op == "*": return l * r
            if op == "/": return l // r if r!=0 else 0
            if op == "<<": return l << r
            if op == ">>": return l >> r
            if op == "&": return l & r
            if op == "|": return l | r
        return 0

    def get_extract_info(node):
        for op in node.get('parser_ops', []):
            if op['op'] == 'extract':
                params = op['parameters']
                # Return: header_name, var_len_expression (if exists)
                return params[0]['value'], (params[1] if len(params) > 1 else None)
        return None, None

    def split_transition_value(hex_str, key_fields, header_type_to_fields, header_inst_to_type):
        widths = []
        for header_name, field_name in key_fields:
            h_type = header_inst_to_type.get(header_name)
            all_fields = header_type_to_fields.get(h_type, [])
            width = next((w for f, w in all_fields if f == field_name), 0)
            widths.append(width)

        total_key_bits = sum(widths)
        hex_bits = len(hex_str) * 4
        # Logic to handle potential padding in JSON hex strings
        is_padded = hex_bits > total_key_bits and (hex_bits % 8 == 0)

        values = []
        current_hex_idx = 0
        for w in widths:
            chunk_bits = max(8, (w + 7) // 8 * 8) if is_padded else w
            chunk_chars = math.ceil(chunk_bits / 4)
            val = hex_str[current_hex_idx : current_hex_idx + chunk_chars]
            values.append(val)
            current_hex_idx += chunk_chars
        return values

    # --- 3. DFS TRAVERSAL ---
    all_paths = []

    def dfs(current_state_name, path_stack):
        state_node = states_dict.get(current_state_name)
        if not state_node or current_state_name == "ingress":
            all_paths.append(list(path_stack))
            return

        header_inst, var_len_expr = get_extract_info(state_node)
        
        transition_keys = []
        for k in state_node.get('transition_key', []):
            transition_keys.append(tuple(k['value']))

        transitions = state_node.get('transitions', [])
        has_explicit = any(t['type'] != 'default' for t in transitions)

        if not transitions:
            if header_inst: 
                path_stack.append({'header': header_inst, 'matches': {}, 'var_expr': var_len_expr})
            all_paths.append(list(path_stack))
            if header_inst: path_stack.pop()
            return

        for t in transitions:
            if has_explicit and t['type'] == 'default':
                continue

            matches = {}
            if t['type'] == 'hexstr':
                full_val = t['value'].replace('0x', '')
                split_vals = split_transition_value(full_val, transition_keys, header_type_to_fields, header_inst_to_type)
                for i, (h_name, f_name) in enumerate(transition_keys):
                    matches[f_name] = split_vals[i]

            if header_inst:
                path_stack.append({'header': header_inst, 'matches': matches, 'var_expr': var_len_expr})

            dfs(t['next_state'], path_stack)
            if header_inst: path_stack.pop()

    init_state = parser['init_state']
    dfs(init_state, [])

    # --- 4. BIT PACKING & OUTPUT ---
    output_file = os.path.join(output_folder, f"{p4_basename}_mask.hex")
    with open(output_file, 'w') as out:
        all_paths.sort(key=len, reverse=True)
        unique_paths = []
        seen = set()
        for p in all_paths:
            sig = str(p)
            if sig in seen: continue
            seen.add(sig)
            unique_paths.append(p)
        
        total_path_dbits = clog2(len(unique_paths))

        for i, path in enumerate(unique_paths):
            
            # A. Build the Giant Bit String (0, 1, or x)
            packet_bit_string = ""
            extracted_tags = []
            
            current_bit_pos = 0

            for node in path:
                h_name = node['header']
                matches = node['matches']
                var_expr = node['var_expr']

                h_type = header_inst_to_type.get(h_name)
                fields = header_type_to_fields.get(h_type, [])
                
                # Dynamic Width Logic
                calculated_var_width = None
                if var_expr:
                    calculated_var_width = evaluate_expression(var_expr, matches)

                for f_index, (f_name, f_width) in enumerate(fields):
                    
                    effective_width = f_width
                    if calculated_var_width is not None and f_index == len(fields) - 1:
                        effective_width = calculated_var_width
                    
                    # 1. Generate Bits for this Field
                    field_bits = ""
                    if f_name in matches:
                        # Convert hex match to binary, pad with 0s to width
                        val_int = int(matches[f_name], 16)
                        # Format as binary string of length 'effective_width'
                        field_bits = format(val_int, f'0{effective_width}b')
                        # Sanity check: if value overflows width (unlikely with correct P4), truncate
                        if len(field_bits) > effective_width:
                            field_bits = field_bits[-effective_width:]
                    else:
                        # No match -> all 'x'
                        field_bits = 'x' * effective_width
                    
                    packet_bit_string += field_bits

                    # 2. Extract Tags (Using the correct packed offsets)
                    tag_match = re.search(r"__([a-zA-Z0-9]+)__", f_name)
                    if tag_match:
                        tag_name = tag_match.group(1)
                        
                        start_offset = current_bit_pos
                        end_offset = current_bit_pos + effective_width - 1
                        
                        msb_index = char_width - 1 - start_offset
                        lsb_index = char_width - 1 - end_offset
                        
                        extracted_tags.append(f"{tag_name}:{msb_index}:{lsb_index}")

                    current_bit_pos += effective_width

            # B. Convert Bit String to Packed Hex
            # We process 4 bits at a time.
            packed_hex = ""
            
            # Pad length to multiple of 4 if needed (though P4 headers are usually byte-aligned)
            remainder = len(packet_bit_string) % 4
            if remainder != 0:
                # Pad with 'x' at the end or '0'? usually x for mask
                packet_bit_string += 'x' * (4 - remainder)

            for j in range(0, len(packet_bit_string), 4):
                nibble = packet_bit_string[j : j+4]
                
                if 'x' in nibble:
                    # If ANY bit is 'x', the whole hex char is a wildcard 'x'
                    # (Conservative Masking)
                    packed_hex += 'x'
                else:
                    # All bits are 0 or 1 -> convert to Hex char
                    val = int(nibble, 2)
                    packed_hex += format(val, 'x')

            packed_hex = adjust_width(packed_hex, char_width//4)
            # --- Output ---
            tags_str = ";".join(extracted_tags)
            final_line = f"{packed_hex} {total_path_dbits}'d{i+1} {tags_str}".strip()
            out.write(final_line + "\n")
            
            if debug:
                print(f"Path {i+1}:")
                print(f"  {final_line}")

def main():
    args = get_p4_to_ir_args()
    generate_packed_hex(args.p4, args.w, args.o, args.debug)

if __name__ == "__main__":
    main()