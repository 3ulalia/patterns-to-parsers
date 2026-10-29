#include "ptp_design.h"
#include <ap_axi_sdata.h>
#include <fstream>
#include <hls_stream.h>
#include <iostream>
#include <string>

void parser_top(hls::stream<ap_axiu<DATA_WIDTH, 0, 0, 0>> &in,
                hls::stream<ap_uint<ID_WIDTH>> &out);
int main() {
  hls::stream<ap_axiu<DATA_WIDTH, 0, 0, 0>> inStream;
  hls::stream<ap_uint<ID_WIDTH>> outStream;

  ap_axiu<DATA_WIDTH, 0, 0, 0> flit;
  flit.keep = -1; // we don't use these; set to all ones
  flit.strb = -1;
  ap_uint<ID_WIDTH> expected_output = 0;
  ap_uint<ID_WIDTH> actual_output = 0;

  std::ifstream flits_file("vec_flits.hex");
  std::ifstream ctrl_file("vec_ctrl.hex");
  std::ifstream exp_file("vec_exp.hex");

  if (flits_file.fail() || ctrl_file.fail() || exp_file.fail()) {
    std::cout << "Failed to open one of the hex files!" << std::endl;
    return 1;
  }

  int flit_index = 0;
  int packet_index = 0;
  int n_mismatches = 0;
  std::string flits_line;
  std::string ctrl_line;
  std::string exp_line;

  while (std::getline(flits_file, flits_line) &&
         std::getline(ctrl_file, ctrl_line)) {

    flit.data = ap_uint<DATA_WIDTH>(flits_line.c_str(), 16);
    flit.last = std::stoi(ctrl_line, nullptr, 16) & 1;

    // write the current flit into the stream
    inStream.write(flit);

    // call the parser
    parser_top(inStream, outStream);

    if (flit.last) { // last flit in the packet: compare outputs

      std::getline(exp_file, exp_line);

      expected_output = ap_uint<ID_WIDTH>(exp_line.c_str(), 16);

      if (outStream.empty()) {
        std::cout << "MISMATCH on packet " << packet_index
                  << ": sent TLAST, but got no output back" << std::endl;
        n_mismatches++;
      } else {
        actual_output = outStream.read();
        // compare the outputs
        if (expected_output != actual_output) {
          std::cout << "MISMATCH on packet " << packet_index << ": expected "
                    << expected_output.to_string(16) << " but got "
                    << actual_output.to_string(16) << std::endl;
          n_mismatches++;
        }
      }
      packet_index++;
    } else if (!outStream.empty()) {
      std::cout << "MISMATCH on packet " << packet_index << ", flit "
                << flit_index << ": didn't send TLAST, but got output back"
                << std::endl;
      outStream.read(); // clear the output
      n_mismatches++;
    }
    flit_index++;
  }

  if (std::getline(ctrl_file, ctrl_line) || std::getline(exp_file, exp_line)) {
    std::cout << "MISMATCH: not all files finished at the same time!"
              << std::endl;
    n_mismatches++;
  }

  if (!inStream.empty()) {
    std::cout << "MISMATCH on packet " << packet_index
              << ": parser stopped consuming" << std::endl;
    n_mismatches++;
  }

  if (n_mismatches == 0) {
    std::cout << "PASS" << std::endl;
  } else {
    std::cout << "FAIL: " << n_mismatches << " mismatches" << std::endl;
  }
  return n_mismatches != 0;
}
