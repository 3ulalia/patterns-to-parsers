// AI-generated (Claude, Anthropic); review before relying on it.
// Hardware counterpart of ptp/hls/tb.cpp: loads the xclbin, streams vec_flits.hex /
// vec_ctrl.hex through mm2s -> parser_top -> s2mm, and checks the IDs against vec_exp.hex.
// Usage: parser_host <xclbin> <vector dir> [device index] [timeout ms]
#include <chrono>
#include <cstdint>
#include <fstream>
#include <iostream>
#include <string>
#include <vector>

#include <xrt/xrt_bo.h>
#include <xrt/xrt_device.h>
#include <xrt/xrt_kernel.h>

static bool read_lines(const std::string &path, std::vector<std::string> &lines) {
  std::ifstream file(path);
  if (file.fail()) {
    std::cout << "Failed to open " << path << std::endl;
    return false;
  }
  std::string line;
  while (std::getline(file, line)) {
    while (!line.empty() && (line.back() == '\r' || line.back() == ' ')) {
      line.pop_back();
    }
    if (!line.empty()) {
      lines.push_back(line);
    }
  }
  return true;
}

static int hex_digit(char character) {
  if (character >= '0' && character <= '9') {
    return character - '0';
  }
  if (character >= 'a' && character <= 'f') {
    return character - 'a' + 10;
  }
  if (character >= 'A' && character <= 'F') {
    return character - 'A' + 10;
  }
  return -1;
}

// MSB-first hex -> little-endian bytes, the memory layout of ap_uint<W>.
static bool hex_to_bytes(const std::string &hex, uint8_t *bytes, size_t n_bytes) {
  for (size_t byte = 0; byte < n_bytes; byte++) {
    bytes[byte] = 0;
    for (size_t nibble = 0; nibble < 2; nibble++) {
      size_t from_end = 2 * byte + nibble;
      if (from_end >= hex.size()) {
        continue;
      }
      int value = hex_digit(hex[hex.size() - 1 - from_end]);
      if (value < 0) {
        return false;
      }
      bytes[byte] |= value << (4 * nibble);
    }
  }
  return true;
}

int main(int argc, char **argv) {
  if (argc < 3) {
    std::cout << "Usage: " << argv[0]
              << " <xclbin> <vector dir> [device index] [timeout ms]" << std::endl;
    return 2;
  }
  std::string xclbin_path = argv[1];
  std::string vector_dir = argv[2];
  unsigned device_index = 0;
  if (argc > 3) {
    device_index = std::stoul(argv[3]);
  }
  int timeout_ms = 5000;
  if (argc > 4) {
    timeout_ms = std::stoi(argv[4]);
  }

  std::vector<std::string> flit_lines, ctrl_lines, expected_lines;
  if (!read_lines(vector_dir + "/vec_flits.hex", flit_lines) ||
      !read_lines(vector_dir + "/vec_ctrl.hex", ctrl_lines) ||
      !read_lines(vector_dir + "/vec_exp.hex", expected_lines)) {
    return 1;
  }
  if (flit_lines.empty() || flit_lines.size() != ctrl_lines.size()) {
    std::cout << "MISMATCH: vec_flits.hex and vec_ctrl.hex differ in length" << std::endl;
    return 1;
  }
  size_t n_flits = flit_lines.size();
  size_t n_packets = expected_lines.size();
  size_t flit_bytes = (flit_lines[0].size() + 1) / 2; // DATA_WIDTH / 8

  auto device = xrt::device(device_index);
  auto uuid = device.load_xclbin(xclbin_path);
  auto mm2s = xrt::kernel(device, uuid, "mm2s");
  auto s2mm = xrt::kernel(device, uuid, "s2mm");

  // group_id(n) is the memory bank of kernel argument n (see connectivity.ini)
  xrt::bo flits_bo(device, n_flits * flit_bytes, mm2s.group_id(0));
  xrt::bo ctrl_bo(device, n_flits, mm2s.group_id(1));
  xrt::bo ids_bo(device, n_packets * sizeof(uint64_t), s2mm.group_id(0));

  auto flits_map = flits_bo.map<uint8_t *>();
  auto ctrl_map = ctrl_bo.map<uint8_t *>();
  for (size_t flit = 0; flit < n_flits; flit++) {
    if (!hex_to_bytes(flit_lines[flit], flits_map + flit * flit_bytes, flit_bytes) ||
        !hex_to_bytes(ctrl_lines[flit], ctrl_map + flit, 1)) {
      std::cout << "Bad hex on vector line " << flit << std::endl;
      return 1;
    }
  }
  flits_bo.sync(XCL_BO_SYNC_BO_TO_DEVICE);
  ctrl_bo.sync(XCL_BO_SYNC_BO_TO_DEVICE);

  // start the consumer first so the parser's output never backs up
  auto start_time = std::chrono::steady_clock::now();
  auto s2mm_run = s2mm(ids_bo, static_cast<int>(n_packets));
  auto mm2s_run = mm2s(flits_bo, ctrl_bo, static_cast<int>(n_flits));
  // one deadline for both kernels: mm2s blocks forever if the parser stops consuming
  auto deadline = start_time + std::chrono::milliseconds(timeout_ms);
  auto remaining = [&deadline]() {
    auto left = std::chrono::duration_cast<std::chrono::milliseconds>(
        deadline - std::chrono::steady_clock::now());
    if (left.count() < 1) {
      return std::chrono::milliseconds(1);
    }
    return left;
  };
  auto mm2s_state = mm2s_run.wait(remaining());
  if (mm2s_state != ERT_CMD_STATE_COMPLETED) {
    std::cout << "MISMATCH: mm2s did not finish (state " << mm2s_state
              << "); the parser stopped consuming flits" << std::endl;
    std::cout << "FAIL: 1 mismatches" << std::endl;
    return 1;
  }
  auto s2mm_state = s2mm_run.wait(remaining());
  auto end_time = std::chrono::steady_clock::now();
  if (s2mm_state != ERT_CMD_STATE_COMPLETED) {
    std::cout << "MISMATCH: s2mm did not finish (state " << s2mm_state
              << "); the parser produced fewer than " << n_packets << " IDs"
              << std::endl;
    std::cout << "FAIL: 1 mismatches" << std::endl;
    return 1;
  }

  ids_bo.sync(XCL_BO_SYNC_BO_FROM_DEVICE);
  auto ids_map = ids_bo.map<uint64_t *>();
  int n_mismatches = 0;
  for (size_t packet = 0; packet < n_packets; packet++) {
    uint64_t expected = std::stoull(expected_lines[packet], nullptr, 16);
    if (ids_map[packet] != expected) {
      if (n_mismatches < 20) {
        std::cout << "MISMATCH on packet " << packet << ": expected " << std::hex
                  << expected << " but got " << ids_map[packet] << std::dec
                  << std::endl;
      }
      n_mismatches++;
    }
  }

  double elapsed_us =
      std::chrono::duration<double, std::micro>(end_time - start_time).count();
  std::cout << n_flits << " flits, " << n_packets << " packets in " << elapsed_us
            << " us (includes kernel launch overhead)" << std::endl;
  if (n_mismatches == 0) {
    std::cout << "PASS" << std::endl;
  } else {
    std::cout << "FAIL: " << n_mismatches << " mismatches" << std::endl;
  }
  return n_mismatches != 0;
}
