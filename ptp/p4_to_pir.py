"""P4 frontend for ptp: P4 program -> PIR (form A or form B).

Differences from the original (ptp_v1) frontend:
  * a row carries ONLY the extraction tags (form B: ``<hex> <tags>``) when the path has tags,
    otherwise ONLY the id (form A: ``<hex> <W>'d<i>``); a file is all-A or all-B.  If some paths
    have tags and others do not, the frontend raises an error listing the paths (a form B row
    needs at least one tag).
  * a tag that occurs several times on one path keeps only its LAST occurrence (the innermost /
    highest-offset one = lowest msb), e.g. IPv6 -> ext1 -> ext2 -> TCP extracts ext2's nextHdr as
    ``prot``.  (The old tool kept the FIRST occurrence = highest msb.)  Repeats are reported in
    the returned info and printed, never an error.
  * id width is clog2(N+1) so that id N always fits.
Tag msb/lsb: ``msb = max_width - 1 - bit_offset`` (bit offset from the first wire bit).

CLI: python3 -m ptp.p4_to_pir <prog.p4> --w <max_width_bits> [--dw 64] -o <outdir>   (p4c from PATH or $P4C)
Writes <outdir>/<name>.pir (hex, form A or B), <outdir>/<name>.json (ptp config).
"""
import argparse
import json
import math
import os
import re
import subprocess
import sys


def clog2(n):
    return math.ceil(math.log2(n)) if n > 0 else 1


def adjust_width(s, l):
    return s[:l] if len(s) > l else s + "x" * (l - len(s))


def dedupe_last(tags):
    """tags: [(name, msb, lsb, header, field)] in path order -> (kept, dropped).  For a repeated
    name only the occurrence with the lowest msb (the last one on the wire) is kept."""
    best = {}
    for t in tags:
        if t[0] not in best or t[1] < best[t[0]][1]:
            best[t[0]] = t
    kept = [t for t in tags if best[t[0]] is t]
    dropped = [t for t in tags if best[t[0]] is not t]
    return kept, dropped


def generate_pir(p4_file, char_width, output_folder, data_width=64, debug=False):
    """Returns (pir_path, json_path, info).  info: form, rows, repeats [(row, tag, dropped, kept)]."""
    p4_path = os.path.abspath(p4_file)
    if not os.path.isfile(p4_path):
        raise FileNotFoundError(f"P4 file not found: {p4_file}")
    p4_basename = os.path.splitext(os.path.basename(p4_path))[0]
    os.makedirs(output_folder, exist_ok=True)
    cmd = [os.environ.get("P4C", "p4c"), "--target", "bmv2", "--arch", "v1model", "-o", output_folder, p4_path]
    try:
        subprocess.run(cmd, check=True, capture_output=not debug)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"p4c failed with exit code {e.returncode}") from e
    json_file = os.path.join(output_folder, f"{p4_basename}.json")
    with open(json_file, 'r') as f:
        data = json.load(f)

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

    # --- 4. BIT PACKING ---
    all_paths.sort(key=len, reverse=True)
    unique_paths, seen = [], set()
    for p in all_paths:
        sig = str(p)
        if sig in seen:
            continue
        seen.add(sig)
        unique_paths.append(p)
    id_w = max(1, len(unique_paths).bit_length())      # ids are 1..N

    lines, forms, repeats = [], [], []
    for i, path in enumerate(unique_paths):
        packet_bit_string = ""
        tags = []
        current_bit_pos = 0
        for node in path:
            h_name = node['header']
            matches = node['matches']
            var_expr = node['var_expr']
            h_type = header_inst_to_type.get(h_name)
            fields = header_type_to_fields.get(h_type, [])
            calculated_var_width = evaluate_expression(var_expr, matches) if var_expr else None
            for f_index, (f_name, f_width) in enumerate(fields):
                effective_width = f_width
                if calculated_var_width is not None and f_index == len(fields) - 1:
                    effective_width = calculated_var_width
                if f_name in matches:
                    val_int = int(matches[f_name], 16)
                    field_bits = format(val_int, f'0{effective_width}b')
                    if len(field_bits) > effective_width:
                        field_bits = field_bits[-effective_width:]
                else:
                    field_bits = 'x' * effective_width
                packet_bit_string += field_bits
                tag_match = re.search(r"__([a-zA-Z0-9]+)__", f_name)
                if tag_match:
                    msb_index = char_width - 1 - current_bit_pos
                    lsb_index = char_width - 1 - (current_bit_pos + effective_width - 1)
                    tags.append((tag_match.group(1), msb_index, lsb_index, h_name, f_name))
                current_bit_pos += effective_width

        kept, dropped = dedupe_last(tags)
        for t in dropped:
            k = next(x for x in kept if x[0] == t[0])
            repeats.append((i + 1, t[0], f"{t[3]}.{t[4]} [{t[1]}:{t[2]}]", f"{k[3]}.{k[4]} [{k[1]}:{k[2]}]"))

        remainder = len(packet_bit_string) % 4
        if remainder:
            packet_bit_string += 'x' * (4 - remainder)
        packed_hex = ""
        for j in range(0, len(packet_bit_string), 4):
            nibble = packet_bit_string[j:j + 4]
            packed_hex += 'x' if 'x' in nibble else format(int(nibble, 2), 'x')
        packed_hex = adjust_width(packed_hex, char_width // 4)
        if kept:
            forms.append('B')
            lines.append(f"{packed_hex} " + ";".join(f"{t[0]}:{t[1]}:{t[2]}" for t in kept))
        else:
            forms.append('A')
            lines.append(f"{packed_hex} {id_w}'d{i + 1}")

    if len(set(forms)) > 1:
        notag = [i + 1 for i, f in enumerate(forms) if f == 'A']
        tagged = [i + 1 for i, f in enumerate(forms) if f == 'B']
        raise ValueError(
            f"{p4_basename}: {len(notag)} path(s) have no extraction tags (paths {notag[:20]}) while "
            f"{len(tagged)} do; a PIR file must be all form A (ids) or all form B (tags). Tag a field on every "
            f"path or none.")
    pir_path = os.path.join(output_folder, f"{p4_basename}.pir")
    with open(pir_path, 'w') as out:
        out.write("\n".join(lines) + "\n")
    json_path = os.path.join(output_folder, f"{p4_basename}.ptp.json")
    with open(json_path, 'w') as jf:
        json.dump({"data_width": data_width, "max_width": char_width, "custom": []}, jf)
    info = {"form": forms[0] if forms else 'A', "rows": len(lines), "repeats": repeats}
    if debug:
        print("\n".join(lines))
    return pir_path, json_path, info


def main(argv=None):
    ap = argparse.ArgumentParser(prog="ptp.p4_to_pir", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("p4")
    ap.add_argument("--w", type=int, required=True, help="max packet width in bits")
    ap.add_argument("--dw", type=int, default=64, help="data width (bits per flit)")
    ap.add_argument("-o", "--outdir", required=True)
    ap.add_argument("--debug", action="store_true")
    a = ap.parse_args(argv)
    pir_path, json_path, info = generate_pir(a.p4, a.w, a.outdir, a.dw, a.debug)
    print(f"wrote {pir_path} ({info['rows']} rows, form {info['form']}) and {json_path}")
    if info["repeats"]:
        print(f"{len(info['repeats'])} repeated tag occurrence(s) dropped (kept the LAST one on each path):")
        agg = {}
        for row, tag, drop, keep in info["repeats"]:
            agg.setdefault((tag, drop.split(' ')[0], keep.split(' ')[0]), []).append(row)
        for (tag, dr, kp), rws in agg.items():
            print(f"  tag {tag}: dropped {dr} in favour of {kp} on {len(rws)} path(s), e.g. rows {rws[:5]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
