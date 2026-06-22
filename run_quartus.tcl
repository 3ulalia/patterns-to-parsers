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

# Top-level: parser_top if it exists in the design dir (fig6), else the
# classifier hex_to_rtl_top (table3). Quartus is only ever run for these two.
if { [file exists "$design_dir/parser_top.sv"] } {
    set top_entity parser_top
} else {
    set top_entity hex_to_rtl_top
}
puts "Top-level entity: $top_entity"
set_global_assignment -name TOP_LEVEL_ENTITY $top_entity

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


# Virtual pin assignments — avoids physical I/O pin exhaustion on wide-bus designs.
# Superset of hex_to_rtl_top and parser_top ports (clk/rst kept as real pins);
# assignments to ports absent from the chosen top are harmless warnings.
set_instance_assignment -name VIRTUAL_PIN ON -to data_in
set_instance_assignment -name VIRTUAL_PIN ON -to data_in_sop
set_instance_assignment -name VIRTUAL_PIN ON -to data_in_eop
set_instance_assignment -name VIRTUAL_PIN ON -to data_in_valid
set_instance_assignment -name VIRTUAL_PIN ON -to data_in_ready
# hex_to_rtl_top-only ports
set_instance_assignment -name VIRTUAL_PIN ON -to path_id_out_ready
set_instance_assignment -name VIRTUAL_PIN ON -to path_id_out
set_instance_assignment -name VIRTUAL_PIN ON -to path_id_out_valid
# parser_top-only ports
set_instance_assignment -name VIRTUAL_PIN ON -to field_ready_in
set_instance_assignment -name VIRTUAL_PIN ON -to field_valid_out
set_instance_assignment -name VIRTUAL_PIN ON -to dip_out
set_instance_assignment -name VIRTUAL_PIN ON -to dp_out
set_instance_assignment -name VIRTUAL_PIN ON -to prot_out
set_instance_assignment -name VIRTUAL_PIN ON -to sip_out
set_instance_assignment -name VIRTUAL_PIN ON -to sp_out

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

