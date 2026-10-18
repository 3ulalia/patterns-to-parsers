import argparse
import copy
import ipaddress
import json
import re
import random
import string

def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--i", required=True, help="input txt file with snort rules")
    parser.add_argument("--o", required=True, help="output PIR file; <name>_config.json and <name>_msg_map.txt are written next to it")
    parser.add_argument("--w", "--width", type=str, required=True, help='Generates mask width in bytes')
    parser.add_argument("--dumpjson", action="store_true", help="Dumps the parsed rules as rules_debug.json next to the output file")
    return parser.parse_args()


def ip_to_hex (ip, version="IPv4"):
    if ip.lower() == "any":
        return "x" * 8 if version == "IPv4" else "x" * 32

    if version == "IPv4":
        return "".join(f"{int(octet):02X}" for octet in ip.split("."))
    else:
        ip_obj = ipaddress.IPv6Address(ip)
        return ip_obj.exploded.replace(":", "").upper()

def port_to_hex (port):
    if port.lower() == "any":
        return "x" * 4
    return f"{int(port):04X}"

def parse_field (field, ip_version="IPv4", is_port=False):
    field = field.strip()
    result = {"TYPE": "NORMAL", "value1": None, "value2": None}

    if not field or field.lower() == "any":
        result["value1"] = "x" * 4 if is_port else ip_to_hex("any", ip_version)
        return result
    
    # Handle IP/Port lists [a,b,c]
    if field.startswith("[") and field.endswith("]"):
        list_content = field[1:-1]
        items = [item.strip() for item in list_content.split(",")]
        result["TYPE"] = "EQOR"
        result["value1"] = items[0]  # Store the list items
        result["value2"] = items  # Store as list for later processing
        # Convert to hex for display purposes
        if is_port:
            result["value1"] = port_to_hex(items[0])
        else:
            result["value1"] = ip_to_hex(items[0], ip_version)
        return result
        
    # Negation
    if field.startswith("!"):
        val = field[1:].strip()
        result["TYPE"] = "NE"
        
        # Handle negated CIDR
        if "/" in val and not is_port:
            network = ipaddress.ip_network(val, strict=False)
            result["value1"] = ip_to_hex(str(network.network_address), ip_version)
            result["value2"] = ip_to_hex(str(network.broadcast_address), ip_version)
        else:
            result["value1"] = port_to_hex(val) if is_port else ip_to_hex(val, ip_version)
        return result
        
    # CIDR
    if "/" in field and not is_port:
        network = ipaddress.ip_network(field, strict=False)
        result["TYPE"] = "RANGE"
        result["value1"] = ip_to_hex(str(network.network_address), ip_version)
        result["value2"] = ip_to_hex(str(network.broadcast_address), ip_version)
        return result
        
    # Port range
    if "-" in field or ":" in field:
        result["TYPE"] = "RANGE"
        sep = "-" if "-" in field else ":"
        start, end = map(str.strip, field.split(sep))
        result["value1"] = port_to_hex(start) if is_port else ip_to_hex(start, ip_version)
        result["value2"] = port_to_hex(end) if is_port else ip_to_hex(end, ip_version)
        return result
        
    # Normal Value
    result["value1"] = port_to_hex(field) if is_port else ip_to_hex(field, ip_version)
    return result

def parse_rule (line):
    line = line.strip()
    # Ignore empty or commented lines
    if not line or line.startswith("#"):
        return None
        
    fields = line.split()
    action = fields[0].upper()
    proto = fields[1].upper()
    ip_version = "IPv6" if proto in ["IPv6", "ICMPV6", "IPV6-ICMP"] else "IPv4"

    # Try and find the arrow in the snort rule
    try:
        arrow_idx = fields.index("->")
    except ValueError:
        print(f"Cannot parse snort rule, missing ->, {line}")
        return None
        
    src_ip = fields[2]
    src_port = fields[3]
    dst_ip = fields[arrow_idx+1]
    dst_port = fields[arrow_idx+2]

    options_str = " ".join(line.split("(", 1)[1:]).rstrip(")") if "(" in line else ""

    rule_json = {
        "action" : action,
        "protocol" : proto,
        "ip_version": ip_version,
        "src_ip" : parse_field(src_ip, ip_version),
        "dst_ip" : parse_field(dst_ip, ip_version),
        "src_port" : parse_field(src_port, ip_version, is_port=True),
        "dst_port" : parse_field(dst_port, ip_version, is_port=True),
        "payload" : {"value": None, "offset": 0},
        "msg": ""
    }

    # Options parsing
    if options_str:
        content_match = re.search(r'content\s*:\s*"([^"]+)"\s*;?', options_str)
        msg_match = re.search(r'msg\s*:\s*"([^"]+)"\s*;?', options_str)
        offset_match = re.search(r'offset\s*:\s*(\d+)\s*;?', options_str)
        depth_match = re.search(r'depth\s*:\s*(\d+)\s*;?', options_str)

        if msg_match:
            rule_json["msg"] = msg_match.group(1)

        if content_match:
            content = content_match.group(1)
            
            # Handle hex pipe notation |XX XX|
            if "|" in content:
                hex_parts = re.findall(r'\|([0-9A-Fa-f\s]+)\|', content)
                content_hex = ""
                for hex_part in hex_parts:
                    content_hex += hex_part.replace(" ", "")
                # Also handle any text outside pipes
                text_parts = re.split(r'\|[0-9A-Fa-f\s]+\|', content)
                for text_part in text_parts:
                    if text_part:
                        content_hex += "".join(f"{ord(c):02X}" for c in text_part)
            else:
                content_hex = "".join(f"{ord(c):02X}" for c in content)
            
            content_length = len(content_hex) // 2  # Convert hex chars to bytes
            
            # Default offset to 0 if not specified
            offset = int(offset_match.group(1)) if offset_match else 0

            if depth_match:
                depth = int(depth_match.group(1))
                last_start = depth - content_length
                json_rules = []

                for start in range(offset, last_start+1):
                    new_rule = copy.deepcopy(rule_json)
                    new_rule["payload"] = {
                        "value": content_hex,
                        "offset": start,
                    }
                    json_rules.append(new_rule)
                return json_rules
            else:  # Only offset or no offset
                rule_json["payload"] = {
                    "value": content_hex,
                    "offset": offset,
                }
                return [rule_json]
    return [rule_json]

def genipv4(prot, src_ip, dst_ip, ihl="5"):
    int_ihl = int(ihl, 16)
    if int_ihl < 5:
        raise Exception ("Invalid ihl value IPv4")
    ipv4str = "0800"+ "4" + str(ihl) + (8*"xx") + prot + (2*"xx") + str(src_ip) + str(dst_ip)
    ipv4str += (int_ihl - 5) * 4 * "xx"
    return ipv4str

def genipv6 (prot, src_ip, dst_ip):
    ipv6str = "86DD" + "6x" + (5*"xx") + prot + "xx" + str(src_ip) + str(dst_ip)
    return ipv6str

def genmac():
    return (12 * "xx")

def gentcp(src_port, dst_port, option="5"):
    int_option = int(option,16)
    if int_option < 5:
        raise Exception ("invalid option TCP")
    tcpstr = str(src_port) + str(dst_port) + (8*"xx") + "5x" + (7*"xx")
    tcpstr += (int_option - 5) * 4 * "xx"
    return tcpstr

def genudp(src_port, dst_port):
    udpstr = str(src_port) + str(dst_port) + 4*"xx"
    return udpstr 

def unique_non_hex_string(input_str: str, existing_ids: list[str]) -> str:
    # Define allowed uppercase letters (non-hex)
    non_hex_chars = ''.join(
        [c for c in string.ascii_uppercase if c not in 'ABCDEF']
    )
    
    existing_set = set(existing_ids)  # Convert to set for faster lookup
    
    while True:
        candidate = ''.join(random.choice(non_hex_chars) for _ in range(len(input_str)))
        if candidate not in existing_set:
            return candidate

def handle_custom_field(rule_field: dict, field_name: str,
                        custom_unique_id_list: list, custom_input_count: int):
    """
    Handles fields that can have special TYPEs like RANGE, NE, or EQOR.
    Returns (final_value, custom_entry_list, updated_count).
    """
    field_type = rule_field.get("TYPE", "")
    value1 = rule_field.get("value1", "")
    if value1 is None:
        value1 = ""
    value2 = rule_field.get("value2", "")
    if value2 is None:
        value2 = ""

    # Handle EQOR (list matching)
    if field_type == "EQOR":
        # value2 contains the list of items
        items = value2 if isinstance(value2, list) else [value1]
        
        # Determine if this is IP or port based on field_name
        is_port = "port" in field_name
        ip_version = "IPv4"  # Default, could be enhanced
        
        # Convert all items to hex
        hex_values = []
        for item in items:
            if is_port:
                hex_values.append(port_to_hex(item))
            else:
                hex_values.append(ip_to_hex(item, ip_version))
        
        custom_id = unique_non_hex_string(value1, custom_unique_id_list)
        custom_unique_id_list.append(custom_id)

        custom_entry = {
            "id": custom_id,
            "type": "const",
            "values": hex_values,
            "opcode": "EQOR"
        }

        return custom_id, custom_entry, custom_input_count + 1

    # Only generate a custom ID for RANGE or NE
    if field_type in ("RANGE", "NE"):
        custom_id = unique_non_hex_string(value1, custom_unique_id_list)
        custom_unique_id_list.append(custom_id)

        custom_entry = {
            "id": custom_id,
            "type": "const",
            "values": [value1, value2],
            "opcode": field_type.upper()
        }

        return custom_id, custom_entry, custom_input_count + 1

    # Default: return the normal value and no config entry
    return value1, None, custom_input_count