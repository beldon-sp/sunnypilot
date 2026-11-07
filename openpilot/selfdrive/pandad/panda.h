#pragma once

#include <cstdint>
#include <ctime>
#include <functional>
#include <list>
#include <memory>
#include <optional>
#include <string>
#include <vector>

#include "openpilot/cereal/gen/cpp/car.capnp.h"
#include "openpilot/cereal/gen/cpp/log.capnp.h"
#include "panda/board/health.h"
#include "panda/board/can.h"
#include "selfdrive/pandad/panda_comms.h"

// legacy health_t layout answered by the pre-sunnypilot DOS firmware shipped
// on tici devices/ clones: carrot-lineage HEALTH_PACKET_VERSION 16, 58 bytes,
// WITHOUT the wake_on_can_pkt slot the mainline v17 struct gained. verified
// field-for-field against the proven dev-tici tree's pandad binary (DWARF:
// health_t byte_size 0x3a, controls_allowed@0x22, safety_mode@0x24,
// alternative_experience@0x2a) — the earlier carrot-mainline 59-byte guess
// shifted every field after @33 by one byte and made the panda read back as
// safety silent instead of byd (permanent "Controls Mismatch" alert).
// get_state() tells the two layouts apart from the control-read size and
// converts legacy replies, so the rest of boardd only ever sees health_t.
struct __attribute__((packed)) legacy_health_t {
  uint32_t uptime_pkt;
  uint32_t voltage_pkt;
  uint32_t current_pkt;
  uint32_t safety_tx_blocked_pkt;
  uint32_t safety_rx_invalid_pkt;
  uint32_t tx_buffer_overflow_pkt;
  uint32_t rx_buffer_overflow_pkt;
  uint32_t faults_pkt;
  uint8_t ignition_line_pkt;
  uint8_t ignition_can_pkt;
  uint8_t controls_allowed_pkt;
  uint8_t car_harness_status_pkt;
  uint8_t safety_mode_pkt;
  uint16_t safety_param_pkt;
  uint8_t fault_status_pkt;
  uint8_t power_save_enabled_pkt;
  uint8_t heartbeat_lost_pkt;
  uint16_t alternative_experience_pkt;
  float interrupt_load_pkt;
  uint8_t fan_power;
  uint8_t safety_rx_checks_invalid_pkt;
  uint16_t spi_error_count_pkt;
  uint8_t fan_stall_count;
  uint16_t sbu1_voltage_mV;
  uint16_t sbu2_voltage_mV;
  uint8_t som_reset_triggered;
};
static_assert(sizeof(legacy_health_t) == 58);

#define USB_TX_SOFT_LIMIT   (0x100U)
#define USBPACKET_MAX_SIZE  (0x40)

#define RECV_SIZE (0x4000U)

#define CAN_REJECTED_BUS_OFFSET   0xC0U
#define CAN_RETURNED_BUS_OFFSET 0x80U

#define PANDA_BUS_OFFSET 4

struct __attribute__((packed)) can_header {
  uint8_t reserved : 1;
  uint8_t bus : 3;
  uint8_t data_len_code : 4;
  uint8_t rejected : 1;
  uint8_t returned : 1;
  uint8_t extended : 1;
  uint32_t addr : 29;
  uint8_t checksum : 8;
};

struct can_frame {
  long address;
  std::string dat;
  long src;
};


class Panda {
private:
  std::unique_ptr<PandaCommsHandle> handle;

public:
  Panda(std::string serial);

  cereal::PandaState::PandaType hw_type = cereal::PandaState::PandaType::UNKNOWN;

  bool connected();
  bool comms_healthy();
  std::string hw_serial();

  // Static functions
  static std::vector<std::string> list();

  // Panda functionality
  cereal::PandaState::PandaType get_hw_type();
  void set_safety_model(cereal::CarParams::SafetyModel safety_model, uint16_t safety_param=0U);
  void set_alternative_experience(uint16_t alternative_experience, uint16_t safety_param_sp=0U);
  std::string serial_read(int port_number = 0);
  void set_uart_baud(int uart, int rate);
  void set_fan_speed(uint16_t fan_speed);
  uint16_t get_fan_speed();
  void set_ir_pwr(uint16_t ir_pwr);
  std::optional<health_t> get_state();
  std::optional<can_health_t> get_can_state(uint16_t can_number);
  void set_loopback(bool loopback);
  std::optional<std::vector<uint8_t>> get_firmware_version();
  bool up_to_date();
  std::optional<std::string> get_serial();
  void set_power_saving(bool power_saving);
  void enable_deepsleep();
  void send_heartbeat(bool engaged, bool engaged_mads);
  void set_can_speed_kbps(uint16_t bus, uint16_t speed);
  void set_can_fd_auto(uint16_t bus, bool enabled);
  void set_data_speed_kbps(uint16_t bus, uint16_t speed);
  void set_canfd_non_iso(uint16_t bus, bool non_iso);
  void can_send(const capnp::List<cereal::CanData>::Reader &can_data_list);
  bool can_receive(std::vector<can_frame>& out_vec);
  void can_reset_communications();

protected:
  // for unit tests
  uint8_t receive_buffer[RECV_SIZE + sizeof(can_header) + 64];
  uint32_t receive_buffer_size = 0;

  Panda() {}
  void pack_can_buffer(const capnp::List<cereal::CanData>::Reader &can_data_list,
                         std::function<void(uint8_t *, size_t)> write_func);
  bool unpack_can_buffer(uint8_t *data, uint32_t &size, std::vector<can_frame> &out_vec);
  uint8_t calculate_checksum(uint8_t *data, uint32_t len);
};
