module axi_skid #(
    parameter int WIDTH = 8
) (
    input  logic              clk,
    input  logic              rst,
    input  logic [WIDTH-1:0]  data_in,
    input  logic              data_in_sop,
    input  logic              data_in_eop,
    input  logic              data_in_valid,
    output logic              data_in_ready,
    output logic [WIDTH-1:0]  data_out,
    output logic              data_out_sop,
    output logic              data_out_eop,
    output logic              data_out_valid,
    input  logic              data_out_ready
);

logic [WIDTH-1:0] data_flop;
logic vld_flop;
logic sop_flop;
logic eop_flop;

always_ff @ (posedge clk or posedge rst) begin
    if (rst) begin
        vld_flop <= 1'b0;
    end else begin
        vld_flop <= data_in_valid;
    end
end

always_ff @ (posedge clk) begin
    data_flop <= data_in;
    sop_flop  <= data_in_sop;
    eop_flop  <= data_in_eop;
end

assign data_out       = data_flop;
assign data_out_sop   = sop_flop;
assign data_out_eop   = eop_flop;
assign data_out_valid = vld_flop;
assign data_in_ready  = ~vld_flop || data_out_ready;
endmodule