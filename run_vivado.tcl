# ============================================================
# run_vivado.tcl — batch flow for one subdesign
# Usage:
#   vivado -mode batch -source run_vivado.tcl -tclargs <base_dir> <design_dir>
# Example:
#   vivado -mode batch -source run_vivado.tcl -tclargs parser_rtl/rtl_sh parser_rtl/rtl_sh/design1
# ============================================================

# ------------------------------------------------------------
# Validate arguments
# ------------------------------------------------------------
if { $argc < 2 } {
    puts "Usage: vivado -mode batch -source run_vivado.tcl -tclargs <base_dir> <design_dir> \[xdc_path\]"
    exit 1
}

set base_dir   [file normalize [lindex $argv 0]]
set design_dir [file normalize [lindex $argv 1]]

# xdc_path: optional 3rd argument; defaults to <base_dir>/../vivado.xdc
if { $argc >= 3 } {
    set xdc_file [file normalize [lindex $argv 2]]
} else {
    set xdc_file "$base_dir/../vivado.xdc"
}

if {![file exists $xdc_file]} {
    puts "Error: vivado.xdc not found at $xdc_file"
    exit 1
}

# ------------------------------------------------------------
# Process this design
# ------------------------------------------------------------
puts "Processing design directory: $design_dir"
puts "Using constraint file: $xdc_file"

set dir_name [file tail $design_dir]
set proj_name "${dir_name}_proj"
set proj_dir "${design_dir}/vivado_proj"

# Create project
create_project $proj_name $proj_dir -part xcvc1902-vsva2197-3HP-e-S -force
#create_project $proj_name $proj_dir -part xcku025-ffva1156-1-c -force
set_property target_language Verilog [current_project]

# Collect SystemVerilog files
set sv_files [glob -nocomplain -directory $design_dir *.sv]
if {[llength $sv_files] == 0} {
    puts "Warning: No SV files found in $design_dir, skipping..."
    close_project
    exit 0
}

add_files -norecurse $sv_files

# top-level entity: optional 4th argument, defaults to parser_top
if { $argc >= 4 } {
    set top_entity [lindex $argv 3]
} else {
    set top_entity "parser_top"
}
set_property top $top_entity [current_fileset]
add_files -fileset constrs_1 $xdc_file

# Optional synthesis settings
#set_property synth_checkpoint_mode None [get_filesets sources_1]
#set_property -name top_auto_set true -objects [get_filesets sources_1]

# Enable out-of-context synthesis
set_property -name {STEPS.SYNTH_DESIGN.ARGS.MORE OPTIONS} -value {-mode out_of_context} -objects [get_runs synth_1]

# ------------------------------------------------------------
# Run synthesis and implementation
# ------------------------------------------------------------
launch_runs synth_1 -jobs 4
wait_on_run synth_1

launch_runs impl_1  -jobs 4
wait_on_run impl_1

open_run impl_1
report_utilization -file "${proj_dir}/utilization.rpt"
report_timing_summary -file "${proj_dir}/timing.rpt"

close_project

puts "Completed processing of: $design_dir"

