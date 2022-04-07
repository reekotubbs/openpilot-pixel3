#from cereal import car
#from common.numpy_fast import mean
#from common.filter_simple import FirstOrderFilter
#from common.realtime import DT_CTRL
#from opendbc.can.can_define import CANDefine
#from selfdrive.car.interfaces import CarStateBase
#from opendbc.can.parser import CANParser
#from selfdrive.config import Conversions as CV
#from selfdrive.car.toyota.values import CAR, DBC, STEER_THRESHOLD, NO_STOP_TIMER_CAR, TSS2_CAR

from cereal import car
from selfdrive.config import Conversions as CV
from common.numpy_fast import mean
from common.filter_simple import FirstOrderFilter
from common.realtime import DT_CTRL
from opendbc.can.can_define import CANDefine
from opendbc.can.parser import CANParser
from selfdrive.car.interfaces import CarStateBase
from selfdrive.car.toyota.values import CAR, DBC, STEER_THRESHOLD, NO_STOP_TIMER_CAR, TSS2_CAR

import time
from selfdrive.car.toyota.linked_ring import LinkedRing

class CarState(CarStateBase):
  def __init__(self, CP):
    super().__init__(CP)
    can_define = CANDefine(DBC[CP.carFingerprint]["pt"])
    self.shifter_values = can_define.dv["GEAR_PACKET"]["GEAR"]

    # On cars with cp.vl["STEER_TORQUE_SENSOR"]["STEER_ANGLE"]
    # the signal is zeroed to where the steering angle is at start.
    # Need to apply an offset as soon as the steering angle measurements are both received
    self.accurate_steer_angle_seen = False
    self.angle_offset = FirstOrderFilter(None, 60.0, DT_CTRL, initialized=False)

    self.low_speed_lockout = False
    self.acc_type = 1

    self.use_zss = CP.carFingerprint == CAR.COROLLA_2010

    self.angle_offset = 0.
    self.last_time = -1
    self.last_angle = 0
    self.last_rate = 0
    self.rolling_sum = LinkedRing(5)

  def update(self, cp, cp_cam):
    ret = car.CarState.new_message()

    ret.doorOpen = any([cp.vl["BODY_CONTROL_STATE"]["DOOR_OPEN_FL"], cp.vl["BODY_CONTROL_STATE"]["DOOR_OPEN_FR"],
                        cp.vl["BODY_CONTROL_STATE"]["DOOR_OPEN_RL"], cp.vl["BODY_CONTROL_STATE"]["DOOR_OPEN_RR"]])
    ret.seatbeltUnlatched = cp.vl["BODY_CONTROL_STATE"]["SEATBELT_DRIVER_UNLATCHED"] != 0
    #ret.parkingBrake = False # cp.vl["BODY_CONTROL_STATE"]["PARKING_BRAKE"] == 1

    ret.brakePressed = cp.vl["BRAKE_MODULE"]["BRAKE_PRESSED"] != 0
    ret.brakeHoldActive = cp.vl["ESP_CONTROL"]["BRAKE_HOLD_ACTIVE"] == 1
    if self.CP.enableGasInterceptor:
      ret.gas = (cp.vl["GAS_SENSOR"]["INTERCEPTOR_GAS"] + cp.vl["GAS_SENSOR"]["INTERCEPTOR_GAS2"]) / 2.
      ret.gasPressed = ret.gas > 15
    else:
      # TODO: find a new, common signal
      msg = "GAS_PEDAL_HYBRID" if (False) else "GAS_PEDAL"
      ret.gas = cp.vl[msg]["GAS_PEDAL"]
      if self.CP.carFingerprint == CAR.COROLLA_2010:
        ret.gasPressed = False
      else:
        ret.gasPressed = cp.vl["PCM_CRUISE"]["GAS_RELEASED"] == 0

    if self.CP.carFingerprint == CAR.COROLLA_2010:
      ret.wheelSpeeds = self.get_wheel_speeds(
        cp.vl["WHEEL_SPEEDS_FRONT"]["WHEEL_SPEED_FL"],
        cp.vl["WHEEL_SPEEDS_FRONT"]["WHEEL_SPEED_FR"],
        cp.vl["WHEEL_SPEEDS_REAR"]["WHEEL_SPEED_RL"],
        cp.vl["WHEEL_SPEEDS_REAR"]["WHEEL_SPEED_RR"],
      )
    else:
      ret.wheelSpeeds = self.get_wheel_speeds(
        cp.vl["WHEEL_SPEEDS"]["WHEEL_SPEED_FL"],
        cp.vl["WHEEL_SPEEDS"]["WHEEL_SPEED_FR"],
        cp.vl["WHEEL_SPEEDS"]["WHEEL_SPEED_RL"],
        cp.vl["WHEEL_SPEEDS"]["WHEEL_SPEED_RR"],
      )
    ret.vEgoRaw = mean([ret.wheelSpeeds.fl, ret.wheelSpeeds.fr, ret.wheelSpeeds.rl, ret.wheelSpeeds.rr])
    ret.vEgo, ret.aEgo = self.update_speed_kf(ret.vEgoRaw)

    ret.standstill = ret.vEgoRaw < 0.001

    if self.use_zss:
      ret.steeringAngleDeg = cp.vl["SECONDARY_STEER_ANGLE"]["ZORRO_STEER"]
      if self.last_time == -1:
        ret.steeringRateDeg = 0  # set to 0 since it isn't moving yet
        self.last_time = round(time.time() * 1000)
      else:
        cur_time = round(time.time() * 1000)
        elapsed = cur_time - self.last_time
        elapsed /= 1000.0
        cur_angle = (ret.steeringAngleDeg - self.last_angle) / elapsed
        self.rolling_sum.add_val(cur_angle)
        ret.steeringRateDeg = cur_angle # self.rolling_sum.average()
        self.last_time = cur_time
      self.last_angle = ret.steeringAngleDeg

    else:
      ret.steeringAngleDeg = cp.vl["STEER_ANGLE_SENSOR"]["STEER_ANGLE"] + cp.vl["STEER_ANGLE_SENSOR"]["STEER_FRACTION"]
      torque_sensor_angle_deg = cp.vl["STEER_TORQUE_SENSOR"]["STEER_ANGLE"]

      # On some cars, the angle measurement is non-zero while initializing
      if abs(torque_sensor_angle_deg) > 1e-3 and not bool(cp.vl["STEER_TORQUE_SENSOR"]["STEER_ANGLE_INITIALIZING"]):
        self.accurate_steer_angle_seen = True

      if self.accurate_steer_angle_seen:
        # Offset seems to be invalid for large steering angles
        if abs(ret.steeringAngleDeg) < 90 and cp.can_valid:
          self.angle_offset.update(torque_sensor_angle_deg - ret.steeringAngleDeg)

        if self.angle_offset.initialized:
          ret.steeringAngleOffsetDeg = self.angle_offset.x
          ret.steeringAngleDeg = torque_sensor_angle_deg - self.angle_offset.x

      ret.steeringRateDeg = cp.vl["STEER_ANGLE_SENSOR"]["STEER_RATE"]

    can_gear = int(cp.vl["GEAR_PACKET"]["GEAR"])
    ret.gearShifter = self.parse_gear_shifter(self.shifter_values.get(can_gear, None))
    ret.leftBlinker = cp.vl["BLINKERS_STATE"]["TURN_SIGNALS"] == 1
    ret.rightBlinker = cp.vl["BLINKERS_STATE"]["TURN_SIGNALS"] == 2

    ret.steeringTorque = cp.vl["STEER_TORQUE_SENSOR"]["STEER_TORQUE_DRIVER"]
    ret.steeringTorqueEps = cp.vl["STEER_TORQUE_SENSOR"]["STEER_TORQUE_EPS"] * 0.88 # * self.eps_torque_scale
    # we could use the override bit from dbc, but it's triggered at too high torque values
    ret.steeringPressed = abs(ret.steeringTorque) > STEER_THRESHOLD
    ret.steerWarning = cp.vl["EPS_STATUS"]["LKA_STATE"] not in [1, 3, 5]

    if self.CP.carFingerprint in (CAR.LEXUS_IS, CAR.LEXUS_RC):
      ret.cruiseState.available = cp.vl["DSU_CRUISE"]["MAIN_ON"] != 0
      ret.cruiseState.speed = cp.vl["DSU_CRUISE"]["SET_SPEED"] * CV.KPH_TO_MS
    elif self.CP.carFingerprint == CAR.COROLLA_2010:
      ret.cruiseState.available = True
      ret.cruiseState.speed = 0
    else:
      ret.cruiseState.available = cp.vl["PCM_CRUISE_2"]["MAIN_ON"] != 0
      ret.cruiseState.speed = cp.vl["PCM_CRUISE_2"]["SET_SPEED"] * CV.KPH_TO_MS

    if self.CP.carFingerprint in TSS2_CAR:
      self.acc_type = cp_cam.vl["ACC_CONTROL"]["ACC_TYPE"]

    # some TSS2 cars have low speed lockout permanently set, so ignore on those cars
    # these cars are identified by an ACC_TYPE value of 2.
    # TODO: it is possible to avoid the lockout and gain stop and go if you
    # send your own ACC_CONTROL msg on startup with ACC_TYPE set to 1
    if (self.CP.carFingerprint not in TSS2_CAR and self.CP.carFingerprint not in (CAR.COROLLA_2010, CAR.LEXUS_IS, CAR.LEXUS_RC)) or \
       (self.CP.carFingerprint in TSS2_CAR and self.acc_type == 1):
      self.low_speed_lockout = cp.vl["PCM_CRUISE_2"]["LOW_SPEED_LOCKOUT"] == 2

    if self.CP.carFingerprint != CAR.COROLLA_2010:
      self.pcm_acc_status = cp.vl["PCM_CRUISE"]["CRUISE_STATE"]
    if self.CP.carFingerprint in NO_STOP_TIMER_CAR or self.CP.enableGasInterceptor:
      # ignore standstill in hybrid vehicles, since pcm allows to restart without
      # receiving any special command. Also if interceptor is detected
      ret.cruiseState.standstill = False
    else:
      ret.cruiseState.standstill = self.pcm_acc_status == 7

    if self.CP.carFingerprint == CAR.COROLLA_2010:
      ret.cruiseState.enabled = bool(cp.vl["PCM_CRUISE_SM"]["CRUISE_CONTROL_STATE"] != 0)
      ret.cruiseState.nonAdaptive = False

      ret.genericToggle = True
      ret.stockAeb = False
    else:
      ret.cruiseState.enabled = bool(cp.vl["PCM_CRUISE"]["CRUISE_ACTIVE"])
      ret.cruiseState.nonAdaptive = cp.vl["PCM_CRUISE"]["CRUISE_STATE"] in (1, 2, 3, 4, 5, 6)

      ret.genericToggle = bool(cp.vl["LIGHT_STALK"]["AUTO_HIGH_BEAM"])
      ret.stockAeb = bool(cp_cam.vl["PRE_COLLISION"]["PRECOLLISION_ACTIVE"] and cp_cam.vl["PRE_COLLISION"]["FORCE"] < -1e-5)

    ret.espDisabled = cp.vl["ESP_CONTROL"]["TC_DISABLED"] != 0
    # 2 is standby, 10 is active. TODO: check that everything else is really a faulty state
    self.steer_state = cp.vl["EPS_STATUS"]["LKA_STATE"]

    if self.CP.enableBsm:
      ret.leftBlindspot = (cp.vl["BSM"]["L_ADJACENT"] == 1) or (cp.vl["BSM"]["L_APPROACHING"] == 1)
      ret.rightBlindspot = (cp.vl["BSM"]["R_ADJACENT"] == 1) or (cp.vl["BSM"]["R_APPROACHING"] == 1)

    return ret

  @staticmethod
  def get_can_parser(CP):
    signals = [
      # sig_name, sig_address
      ("STEER_ANGLE", "STEER_ANGLE_SENSOR", 0),
      ("GEAR", "GEAR_PACKET", 0),
      ("BRAKE_PRESSED", "BRAKE_MODULE", 0),
      ("DOOR_OPEN_FL", "BODY_CONTROL_STATE", 1),
      ("DOOR_OPEN_FR", "BODY_CONTROL_STATE", 1),
      ("DOOR_OPEN_RL", "BODY_CONTROL_STATE", 1),
      ("DOOR_OPEN_RR", "BODY_CONTROL_STATE", 1),
      ("SEATBELT_DRIVER_UNLATCHED", "BODY_CONTROL_STATE", 1),
      ("PARKING_BRAKE", "BODY_CONTROL_STATE", 0),
      ("TC_DISABLED", "ESP_CONTROL", 1),
      ("BRAKE_HOLD_ACTIVE", "ESP_CONTROL", 0),
      ("STEER_TORQUE_DRIVER", "STEER_TORQUE_SENSOR", 0),
      ("STEER_TORQUE_EPS", "STEER_TORQUE_SENSOR", 0),
      ("STEER_ANGLE", "STEER_TORQUE_SENSOR", 0),
      ("STEER_ANGLE_INITIALIZING", "STEER_TORQUE_SENSOR", 0),
      ("TURN_SIGNALS", "BLINKERS_STATE", 3),
      ("LKA_STATE", "EPS_STATUS", 0),
      ("AUTO_HIGH_BEAM", "LIGHT_STALK", 0),
    ]

    checks = [
      ("GEAR_PACKET", 1),
      ("LIGHT_STALK", 1),
      ("BLINKERS_STATE", 0.15),
      ("BODY_CONTROL_STATE", 3),
      ("ESP_CONTROL", 3),
      ("EPS_STATUS", 25),
      ("BRAKE_MODULE", 40),
      ("STEER_ANGLE_SENSOR", 80),
      ("STEER_TORQUE_SENSOR", 50),
    ]

    if False:
      signals.append(("GAS_PEDAL", "GAS_PEDAL_HYBRID", 0))
      checks.append(("GAS_PEDAL_HYBRID", 33))
    else:
      signals.append(("GAS_PEDAL", "GAS_PEDAL", 0))
      checks.append(("GAS_PEDAL", 33))

    if CP.carFingerprint in (CAR.LEXUS_IS, CAR.LEXUS_RC):
      signals.append(("MAIN_ON", "DSU_CRUISE", 0))
      signals.append(("SET_SPEED", "DSU_CRUISE", 0))
      checks.append(("DSU_CRUISE", 5))
    else:
      if CP.carFingerprint != CAR.COROLLA_2010:
        signals.append(("MAIN_ON", "PCM_CRUISE_2", 0))
        signals.append(("SET_SPEED", "PCM_CRUISE_2", 0))
        signals.append(("LOW_SPEED_LOCKOUT", "PCM_CRUISE_2", 0))
        checks.append(("PCM_CRUISE_2", 33))

    # add gas interceptor reading if we are using it
    if CP.enableGasInterceptor:
      signals.append(("INTERCEPTOR_GAS", "GAS_SENSOR", 0))
      signals.append(("INTERCEPTOR_GAS2", "GAS_SENSOR", 0))
      checks.append(("GAS_SENSOR", 50))

    if CP.enableBsm:
      signals += [
        ("L_ADJACENT", "BSM", 0),
        ("L_APPROACHING", "BSM", 0),
        ("R_ADJACENT", "BSM", 0),
        ("R_APPROACHING", "BSM", 0),
      ]
      checks.append(("BSM", 1))

    if CP.carFingerprint != CAR.COROLLA_2010:
      signals.append(("WHEEL_SPEED_FL", "WHEEL_SPEEDS", 0))
      signals.append(("WHEEL_SPEED_FR", "WHEEL_SPEEDS", 0))
      signals.append(("WHEEL_SPEED_RL", "WHEEL_SPEEDS", 0))
      signals.append(("WHEEL_SPEED_RR", "WHEEL_SPEEDS", 0))
      signals.append(("CRUISE_ACTIVE", "PCM_CRUISE", 0))
      signals.append(("CRUISE_STATE", "PCM_CRUISE", 0))
      signals.append(("GAS_RELEASED", "PCM_CRUISE", 1))
      signals.append(("STEER_FRACTION", "STEER_ANGLE_SENSOR", 0))
      signals.append(("STEER_RATE", "STEER_ANGLE_SENSOR", 0))

      checks.append(("PCM_CRUISE", 33))
      checks.append(("WHEEL_SPEEDS", 80))

    if CP.carFingerprint == CAR.COROLLA_2010:
      signals.append(("ZORRO_STEER", "SECONDARY_STEER_ANGLE", 0))
      signals.append(("CRUISE_CONTROL_STATE", "PCM_CRUISE_SM", 0))
      signals.append(("WHEEL_SPEED_FL", "WHEEL_SPEEDS_FRONT", 0))
      signals.append(("WHEEL_SPEED_FR", "WHEEL_SPEEDS_FRONT", 0))
      signals.append(("WHEEL_SPEED_RL", "WHEEL_SPEEDS_REAR", 0))
      signals.append(("WHEEL_SPEED_RR", "WHEEL_SPEEDS_REAR", 0))

      checks.append(("PCM_CRUISE_SM", 1))
      checks.append(("WHEEL_SPEEDS_FRONT", 1))
      checks.append(("WHEEL_SPEEDS_REAR", 1))
      checks.append(("SECONDARY_STEER_ANGLE", 1))

    return CANParser(DBC[CP.carFingerprint]["pt"], signals, checks, 0)

  @staticmethod
  def get_cam_can_parser(CP):
    signals = [
      ("FORCE", "PRE_COLLISION", 0),
      ("PRECOLLISION_ACTIVE", "PRE_COLLISION", 0),
    ]

    # use steering message to check if panda is connected to frc
    checks = [
      ("STEERING_LKA", 42),
      ("PRE_COLLISION", 0), # TODO: figure out why freq is inconsistent
    ]

    if CP.carFingerprint in TSS2_CAR:
      signals.append(("ACC_TYPE", "ACC_CONTROL"))
      checks.append(("ACC_CONTROL", 33))

    if CP.carFingerprint == CAR.COROLLA_2010:
      signals = []
      checks = []

    return CANParser(DBC[CP.carFingerprint]["pt"], signals, checks, 2)
