import json
import sys
import os
from utils.utils import (
    load_graph_config,
    get_mask_parse_args,
    dfs_v2,
    encode_entries,
    clog2
)

def build_mask(graph_json, width_str, outfile, extract_enabled, debug):
    
    # Parse width as integer (now represents character count, not bits)
    char_width = int(width_str)

    graph = load_graph_config(graph_json)
    root = next(iter(graph))
    
    # Trace paths - ALWAYS extract data regardless of flag
    dfs_results = dfs_v2(graph, root, extract_data=True)
    paths, extractions = zip(*dfs_results) if dfs_results else ([], [])

    if debug:
        print(f"DEBUG: Found {len(paths)} paths.")

    # Get encoded strings (now character-based)
    result_strings = encode_entries(paths, char_width)

    # Calculate input bus width (char_width * 4 bits per hex char)
    bus_width = char_width * 4

    # Prepare extraction strings for the 3rd column
    extraction_strs = []
    
    for i, extract_list in enumerate(extractions):
        # Extract list is tuples of (start, end, field_id) in DFS order (LSB-based relative to node start)
        # We need to convert them to Bus MSB/LSB
        
        row_extractions = []
        if extract_enabled and extract_list:
            for start, end, field_id in extract_list:
                # Convert from DFS positions (LSB-based) to actual bus positions
                # DFS: [start, end) where start is closer to LSB
                # Bus: MSB = bus_width - 1, LSB = 0
                
                # Actual MSB of field = (bus_width - 1) - start
                # Actual LSB of field = (bus_width - 1) - end + 1 = bus_width - end
                actual_msb = (bus_width - 1) - start
                actual_lsb = bus_width - end
                
                # Format: field:msb:lsb
                row_extractions.append(f"{field_id}:{actual_msb}:{actual_lsb}")
        
        # Join with semicolon for Option B
        extraction_strs.append(";".join(row_extractions))

    # Generate the Mask File with Optional 3rd Column
    bitw = clog2(len(result_strings))
    
    with open(outfile, 'w') as f:
        for i, s in enumerate(result_strings):
            path_id_hex = f"{bitw}'d{i+1}"
            
            # Construct line: HEX ID [EXTRACTIONS]
            line = f"{s} {path_id_hex}"
            
            if extraction_strs[i]:
                line += f" {extraction_strs[i]}"
            
            f.write(line + "\n")
    
    print(f"======= Mask generated =======")
    print(f"  - Output file: {outfile}")
    print(f"  - Total paths: {len(result_strings)}")
    print(f"  - Counter width: {bitw} bits")
    print(f"  - Extraction column included: {extract_enabled}")

    return bitw

def main():
    args = get_mask_parse_args()
    build_mask(args.g, args.w, args.o, args.extract, args.debug)

if __name__ == "__main__":
    main()