# =========================================================
# run_quartus.tcl
# Quartus batch setup for a single design directory
# Usage:
#   quartus_sh -t run_quartus.tcl <design_dir> [device]
#   quartus_sh --flow compile <project_name>
# =========================================================

# ---------------------------
# 1. Parse arguments
# ---------------------------
if { $argc < 1 } {
    puts "Usage: quartus_sh -t run_quartus.tcl <design_dir> \[device\]"
    exit 1
}

# Absolute path of design directory
set design_dir [file normalize [lindex $argv 0]]
set dir_name [file tail $design_dir]
set proj_name "${dir_name}_proj"
set proj_dir "${design_dir}/quartus_proj"

# device: optional 2nd argument; defaults to Agilex 5 A5DD064AB32BI1V
if { $argc >= 2 } {
    set fpga_device [lindex $argv 1]
} else {
    set fpga_device "A5DD064AB32BI1V"
}

file mkdir $proj_dir
cd $proj_dir

puts "Setting up project: $proj_name"
puts "Design directory: $design_dir"

# ---------------------------
# 2. Create project
# ---------------------------
project_new $proj_name -overwrite

# ---------------------------
# 3. Family, device, top-level
# ---------------------------
puts "Using Quartus device: $fpga_device"
set_global_assignment -name FAMILY "Agilex 5"
set_global_assignment -name DEVICE $fpga_device
set_global_assignment -name TOP_LEVEL_ENTITY hex_to_rtl_top

# ---------------------------
# 4. Add Verilog / SV files
# ---------------------------
set verilog_files {}
foreach f [glob -nocomplain -directory $design_dir *.v *.sv] {
    if {[file tail $f] ne "hex_to_rtl_top_tb.sv"} {
        lappend verilog_files $f
    }
}

if {[llength $verilog_files] == 0} {
    puts "Warning: No Verilog or SystemVerilog files found in $design_dir"
    project_close
    exit 0
}

foreach f $verilog_files {
    puts "Adding source file: $f"
    set_global_assignment -name SYSTEMVERILOG_FILE $f
}


if {[file exists "$design_dir/../../../quartus.sdc"]} {
    set_global_assignment -name SDC_FILE "$design_dir/../../../quartus.sdc"
} else {
    puts stderr "ERROR: SDC file not found at $design_dir/../../../quartus.sdc"
    exit 1
}


# Example virtual pin assignments (if needed)
set_instance_assignment -name VIRTUAL_PIN ON -to sip_o
set_instance_assignment -name VIRTUAL_PIN ON -to dip_o
set_instance_assignment -name VIRTUAL_PIN ON -to sp_o
set_instance_assignment -name VIRTUAL_PIN ON -to dp_o
set_instance_assignment -name VIRTUAL_PIN ON -to rst
set_instance_assignment -name VIRTUAL_PIN ON -to data_i
set_instance_assignment -name VIRTUAL_PIN ON -to data_sop_i
set_instance_assignment -name VIRTUAL_PIN ON -to clk

# ---------------------------
# 5. Finalize
# ---------------------------
project_close

puts "\nProject setup complete for: $proj_name"
puts "Now run compilation with:"
puts "  quartus_sh --flow compile $proj_name"
puts ""
puts "Or for step-by-step:"
puts "  quartus_syn $proj_name"
puts "  quartus_fit $proj_name"
puts "  quartus_asm $proj_name"
puts "  quartus_sta $proj_name --model=fast --export_settings=off"
puts "  mv ${proj_name}_sta.rpt ${proj_name}_sta_fast.rpt"
puts "  quartus_sta $proj_name --export_settings=off"
puts "  mv ${proj_name}_sta.rpt ${proj_name}_sta_slow.rpt"

