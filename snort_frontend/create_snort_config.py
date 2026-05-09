import math
import json
import os
from snort_utils import (
    parse_args,
    parse_rule,
    genipv4,
    genipv6,
    genmac,
    gentcp,
    genudp,
    unique_non_hex_string,
    handle_custom_field
)


def txt_to_json (txt_file, json_file, dump_json=False):
    rules_json = []

    with open (txt_file, "r") as f:
        for line in f:
            parsed = parse_rule(line)
            if parsed:
                rules_json.extend(parsed)

    #if dumping to a json file, then write
    if dump_json:
        with open (json_file, "w") as f:
            json.dump(rules_json, f, indent=2)
        print(f"Converted {len(rules_json)} rules to JSON: {json_file}")
        
    # print(rules_json)
    return rules_json

def rules_to_hex_streams(rules_json, pkt_len_bytes, outfile):
    # Map actions to 3-bit binary codes
    action_map = {
        "ALERT": "000",
        "LOG": "001",
        "PASS": "010",
        "DROP": "011",
        "REJECT": "100",
        "SDROP": "101",
    }

    hex_streams = []
    action_encodings = []

    custom_unique_id_list = []
    custom_input_count = 0
    config_json_custom_entry = []
    config_json_custom_entry_list = []

    num_rules = len(rules_json)
    if num_rules == 0:
        return [], []

    unique_msgs = set(rule["msg"] for rule in rules_json if rule["msg"] != "")
    msg_to_id = {msg: idx + 1 for idx, msg in enumerate(unique_msgs)}
    msg_to_id[""] = 0  # assign 0 to empty messages


    bits_for_rules = max(1, math.ceil(math.log2(num_rules)))
    bits_for_action = 3
    total_bits = 2*bits_for_rules + bits_for_action #2* because msgid will be equal to bits_for_rules
    # Round total width up to next multiple of 8 for nice byte-aligned SV literal
    total_width = total_bits #((total_bits + 7) // 8) * 8

    for idx, rule in enumerate(rules_json):
        # flatten if parse_rule returned list-in-list structure
        if isinstance(rule, list):
            rule = rule[0]

        ip_version = rule["ip_version"]
        proto = rule["protocol"].upper()

        # --- Protocol number ---
        if proto in ("TCP",):
            proto_hex = "06"
        elif proto in ("UDP",):
            proto_hex = "11"
        elif proto in ("ICMP",):
            proto_hex = "01"
        elif proto in ("ICMPV6", "IPV6-ICMP"):
            proto_hex = "3A"
        else:
            proto_hex = "00"

        #checking RANGES, and NEG
        src_ip, entry, custom_input_count = handle_custom_field(
            rule["src_ip"], "src_ip", custom_unique_id_list, custom_input_count
        )
        if entry:
            config_json_custom_entry_list.append(entry)

        dst_ip, entry, custom_input_count = handle_custom_field(
            rule["dst_ip"], "dst_ip", custom_unique_id_list, custom_input_count
        )
        if entry:
            config_json_custom_entry_list.append(entry)

        src_port, entry, custom_input_count = handle_custom_field(
            rule["src_port"], "src_port", custom_unique_id_list, custom_input_count
        )
        if entry:
            config_json_custom_entry_list.append(entry)

        dst_port, entry, custom_input_count = handle_custom_field(
            rule["dst_port"], "dst_port", custom_unique_id_list, custom_input_count
        )
        if entry:
            config_json_custom_entry_list.append(entry)

        l2 = genmac()
        l3 = genipv4(proto_hex, src_ip, dst_ip) if ip_version == "IPv4" else genipv6(proto_hex, src_ip, dst_ip)
        l4 = gentcp(src_port, dst_port) if proto in ("TCP") else genudp(src_port, dst_port)

        # --- Payload ---
        payload_info = rule.get("payload", {})
        payload_hex = ""
        if payload_info.get("value"):
            offset_bytes = payload_info.get("offset", 0)
            payload_hex = "xx" * offset_bytes + payload_info["value"]

        # Build the packet hex stream (no spaces)
        packet = (
            l2
            + l3
            + l4
            + payload_hex
        )

        # Normalize to target length (bytes → nibbles ×2)
        target_nibbles = pkt_len_bytes * 2
        if len(packet) < target_nibbles:
            packet = packet + ("x" * (target_nibbles - len(packet)))
        else:
            packet = packet[:target_nibbles]

        hex_streams.append(packet)

        # --- Action encoding ---
        action_bits = action_map.get(rule["action"].upper(), "000")
        rule_bits = format(idx, f"0{bits_for_rules}b")
        msg_id = msg_to_id.get(rule.get("msg", ""), 0)
        msg_bits = format(msg_id, f"0{bits_for_rules}b")
        encoding = f"{total_width}'b{rule_bits}{action_bits}{msg_bits}"
        action_encodings.append(encoding)

    #output hex file
    with open (outfile, "w") as f:
        for entry1, entry2 in zip(hex_streams, action_encodings):
            f.write (f"{entry1} {entry2}\n")

    base,ext = os.path.splitext(outfile)
    #output a msg id mapping txt file
    if msg_to_id:  # only proceed if the dict is not empty
        with open(f"{base}_msg_map.txt", "w") as f:
            f.write("ID    MSG\n")  # header line
            for msg, id in msg_to_id.items():
                if msg != "":
                    f.write(f"{id}    {msg}\n")

    
    #output config json
    with open (f"{base}_config.json", "w") as f:
        json.dump({
            "num_cols": 1,
            "char_per_col": target_nibbles,
            "col_per_file": 1,
            "custom": config_json_custom_entry_list
        }, f, indent=2)


if __name__ == "__main__":
    args = parse_args()

    rules_json = txt_to_json(args.i, "out.json", args.dumpjson)
    rules_to_hex_streams(rules_json, int(args.w), args.o)
    
