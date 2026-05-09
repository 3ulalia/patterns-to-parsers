import sys
import json
import shutil
from pathlib import Path

sys.path.append(str(Path(__file__).parent))

from utils.utils import (
    get_top_parse_args,
    jj_render
)

try:
    from tools import build_mask
    from tools import build_rtl_countluts
except ImportError as e:
    print(f"Import Error: {e}")
    print("Ensure you have moved 'build_mask.py' and 'build_rtl_countluts.py' to 'tools/',")
    print("and 'mask_utils.py' to 'src/'.")
    sys.exit(1)

def run_step_1(args, tmp_dir):
    """
    Step 1: Mask Generation
    - Inputs: json graph, max_packet_width (bits)
    - Outputs: tmp/mask.hex
    """
    print("\n[Step 1] Starting Mask Generation...")

    input_json = Path(args.g).resolve()
    if not input_json.exists():
        raise FileNotFoundError(f"Input JSON not found: {input_json}")
    
    mask_hex_path = tmp_dir / "mask.hex"
    mw_chars = args.mw // 4

    try:
        # build_mask now returns path_id_width (bitw), but we might not need it here anymore
        # as build_rtl_countluts recalculates it from the file.
        _ = build_mask.build_mask(
            graph_json=str(input_json),
            width_str=str(mw_chars),
            outfile=str(mask_hex_path),
            extract_enabled=True, 
            debug=args.debug
        )
        print(f"  Success: Mask generated at {mask_hex_path}")
        return mask_hex_path

    except Exception as e:
        print(f"  Error during Step 1: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

def run_step_2(args, mask_path, tmp_dir):
    """
    Step 2: Comparison, Lookup, and Top-Level RTL Generation
    - Generates config.json including data width and max width.
    - Runs build_rtl_countluts to generate all RTL files.
    """
    print("\n[Step 2] Starting RTL Generation...")

    # 1. Calculate Config Values
    num_cols = (args.mw * args.cm) // args.dw
    cpc_divisor = 4 * args.cm

    if args.dw % cpc_divisor != 0:
        raise ValueError(f"Error: Choose a column multiplier such that Data Width ({args.dw}) is divisible by 4 * column multipler.")
        sys.exit(1)

    char_per_col = args.dw // cpc_divisor
    
    # 2. Construct Config Dictionary
    # We inject 'data_width' and 'max_width' so build_rtl_countluts can generate parser_top
    config_data = {
        "num_cols": num_cols,
        "char_per_col": char_per_col,
        "col_per_file": (1 * args.cm),
        "data_width": args.dw,
        "max_width": args.mw,
        "custom": []        
    }

    config_path = tmp_dir / "config.json"
    
    with open(config_path, 'w') as f:
        json.dump(config_data, f, indent=4)
    
    if args.debug:
        print(f"  Generated Config: {config_path}")
        print(json.dumps(config_data, indent=2))

    # 3. Run build_rtl_countluts
    try:
        build_rtl_countluts.build_rtl_countluts(
            cfgfilename=str(config_path),
            mask=str(mask_path),
            countluts=False,
            rtl=True,
            tree1=True,
            tree2=True,
            retime=False,
            unique1=False,
            unique2=False,
            outfile_dir=str(tmp_dir),
            debug=args.debug
        )
        print(f"  Success: RTL files generated in {tmp_dir}")
        
    except Exception as e:
        print(f"  Error during Step 2: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

def main():
    args = get_top_parse_args()

    # --- Validation ---
    if args.dw % 4 != 0:
        print(f"Error: Data Width (--dw {args.dw}) must be a multiple of 4.")
        sys.exit(1)

    if args.mw % 4 != 0:
        print(f"Error: Max Packet Width (--mw {args.mw}) must be a multiple of 4.")
        sys.exit(1)
    
    if args.mw % args.dw != 0:
        print(f"Error: Max Packet Width ({args.mw}) must be divisible by Data Width ({args.dw}).")
        sys.exit(1)
    
    if args.cm <= 0:
        print(f"Error: Column multipler cannot be zero or negative. Passed ({args.cm}).")
        sys.exit(1)

    # --- Setup Workspace ---
    tmp_dir = Path(__file__).parent / "tmp"
    tmp_dir.mkdir(exist_ok=True)

    # --- Step 1 Execution ---
    mask_path = run_step_1(args, tmp_dir)

    # --- Step 2: RTL Generation (Comparison, Lookup, Extractor, Top) ---
    run_step_2(args, mask_path, tmp_dir)

    # --- Post-Processing ---
    out_dir = Path(args.o)
    out_dir.mkdir(exist_ok=True, parents=True)

    print(f"\n[Post-Processing] Moving generated RTL files to {out_dir}...")
    for sv_file in tmp_dir.glob("*.sv"):
        dest = out_dir / sv_file.name
        if dest.exists():
            dest.unlink() 
        shutil.move(str(sv_file), str(dest))
        
    print(f"  Success: All RTL files are now in {out_dir}")

if __name__ == "__main__":
    main()
