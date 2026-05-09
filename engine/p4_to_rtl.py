import sys
import json
import shutil
from pathlib import Path

sys.path.append(str(Path(__file__).parent))

from utils.utils import get_p4_to_rtl_args

try:
    from tools import p4_to_ir
    from tools import build_rtl_countluts
except ImportError as e:
    print(f"Import Error: {e}")
    sys.exit(1)


def run_step_1(args, tmp_dir):
    """P4 -> mask hex via p4c + DFS traversal."""
    print("\n[Step 1] Compiling P4 to mask hex...")

    p4_path = Path(args.p4).resolve()
    if not p4_path.exists():
        raise FileNotFoundError(f"P4 file not found: {p4_path}")

    try:
        p4_to_ir.generate_packed_hex(
            p4_file=str(p4_path),
            width_str=str(args.mw),  # p4_to_ir expects bits; divides by 4 internally
            output_folder=str(tmp_dir),
            debug=args.debug,
        )
        mask_path = tmp_dir / f"{p4_path.stem}_mask.hex"
        if not mask_path.exists():
            raise FileNotFoundError(f"Expected mask file not found: {mask_path}")
        print(f"  Success: Mask generated at {mask_path}")
        return mask_path
    except Exception as e:
        print(f"  Error during Step 1: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


def run_step_2(args, mask_path, tmp_dir):
    """Mask hex -> SystemVerilog RTL."""
    print("\n[Step 2] Starting RTL Generation...")

    num_cols = (args.mw * args.cm) // args.dw
    cpc_divisor = 4 * args.cm

    if args.dw % cpc_divisor != 0:
        raise ValueError(
            f"Data Width ({args.dw}) must be divisible by 4 * column_multiplier ({cpc_divisor})."
        )

    char_per_col = args.dw // cpc_divisor

    config_data = {
        "num_cols": num_cols,
        "char_per_col": char_per_col,
        "col_per_file": args.cm,
        "data_width": args.dw,
        "max_width": args.mw,
        "custom": [],
    }

    config_path = tmp_dir / "config.json"
    with open(config_path, "w") as f:
        json.dump(config_data, f, indent=4)

    if args.debug:
        print(f"  Config: {json.dumps(config_data, indent=2)}")

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
            debug=args.debug,
        )
        print(f"  Success: RTL files generated in {tmp_dir}")
    except Exception as e:
        print(f"  Error during Step 2: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


def main():
    args = get_p4_to_rtl_args()

    if args.dw % 4 != 0:
        print(f"Error: Data Width ({args.dw}) must be a multiple of 4.")
        sys.exit(1)
    if args.mw % 4 != 0:
        print(f"Error: Max Packet Width ({args.mw}) must be a multiple of 4.")
        sys.exit(1)
    if args.mw % args.dw != 0:
        print(f"Error: Max Packet Width ({args.mw}) must be divisible by Data Width ({args.dw}).")
        sys.exit(1)
    if args.cm <= 0:
        print(f"Error: Column multiplier must be positive.")
        sys.exit(1)

    tmp_dir = Path(__file__).parent / "tmp"
    tmp_dir.mkdir(exist_ok=True)

    mask_path = run_step_1(args, tmp_dir)
    run_step_2(args, mask_path, tmp_dir)

    out_dir = Path(args.o)
    out_dir.mkdir(exist_ok=True, parents=True)

    print(f"\n[Post-Processing] Moving RTL files to {out_dir}...")
    for sv_file in tmp_dir.glob("*.sv"):
        dest = out_dir / sv_file.name
        if dest.exists():
            dest.unlink()
        shutil.move(str(sv_file), str(dest))

    print(f"  Done: {out_dir}")


if __name__ == "__main__":
    main()
