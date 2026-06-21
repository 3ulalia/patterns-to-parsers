create_clock -name clk -period 2.000 [get_ports clk] -waveform {0.000 1.000}
set_input_delay  -clock clk 0.0 [get_ports {rst data_in data_in_sop data_in_eop data_in_valid field_ready_in}]
set_output_delay -clock clk 0.0 [get_ports {data_in_ready field_valid_out *_out}]