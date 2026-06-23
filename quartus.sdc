# Clock constraint for Quartus (SDC format)
# 500 MHz => period = 2 ns

# Create a primary input clock
create_clock -name clk -period 2.0 [get_ports clk] -waveform {0 1}

