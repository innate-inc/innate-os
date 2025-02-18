// Copyright 2021 ROBOTIS CO., LTD.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.
//
// Author: LD Robot, Will Son

#ifndef LIPKG_H_
#define LIPKG_H_
#include <stdint.h>
#include <vector>
#include <array>
#include <iostream>
#include <sensor_msgs/msg/laser_scan.hpp>
#include <rclcpp/rclcpp.hpp>
#include "../include/pointdata.h"

#define ANGLE_TO_RADIAN(angle) ((angle) * 3141.59 / 180000)
#define RADIAN_TO_ANGLE(angle) ((angle) * 180000 / 3141.59)

enum
{
  PKG_HEADER = 0x54,
  PKG_VER_LEN = 0x2C,
  POINT_PER_PACK = 12,
};


typedef struct  __attribute__((packed))
{
  uint16_t distance;
  uint8_t confidence;
} LidarPointStructDef;

typedef struct  __attribute__((packed))
{
  uint8_t header;
  uint8_t ver_len;
  uint16_t speed;
  uint16_t start_angle;
  LidarPointStructDef point[POINT_PER_PACK];
  uint16_t end_angle;
  uint16_t timestamp;
  uint8_t crc8;
} LiDARFrameTypeDef;


typedef std::vector < PointData > Points2D;

struct FrameData
{
  float angle_min;
  float angle_max;
  uint32_t len;
  std::vector < uint16_t > distance;
  std::vector < uint8_t > intensities;
};


class LiPkg
{
public:
  LiPkg();
  // Lidar spin speed (Hz)
  double GetSpeed(void);
  // time stamp of the packet
  uint16_t GetTimestamp(void) {return mTimestamp;}
  // a packet is ready
  bool IsPkgReady(void) {return mIsPkgReady;}
  // Lidar data frame is ready
  bool IsFrameReady(void) {return mIsFrameReady;}
  void ResetFrameReady(void) {mIsFrameReady = false;}
  // the number of errors in parser process of lidar data frame
  int32_t GetErrorTimes(void) {return mErrorTimes;}
  // original data package
  const std::array < PointData, POINT_PER_PACK > & GetPkgData(void);
  // parse single packet
  bool Parse(const uint8_t * data, int32_t len);
  // transform raw data to stantard data
  virtual void Transform(std::vector < PointData > & tmp) = 0;
  // combine stantard data into data frames and calibrate
  bool AssemblePacket();
  const FrameData & GetFrameData(void) {mIsFrameReady = false; return mFrameData;}
  sensor_msgs::msg::LaserScan GetLaserScan() {return output;}
  void setStamp(rclcpp::Time timeStamp) {output.header.stamp = timeStamp;}

private:
  uint16_t mTimestamp;
  double mSpeed;
  int32_t mErrorTimes;
  bool mIsFrameReady;
  bool mIsPkgReady;
  std::vector < uint8_t > mDataTmp;
  std::array < PointData, POINT_PER_PACK > mOnePkg;
  std::vector < PointData > mFrameTemp;
  FrameData mFrameData;
  sensor_msgs::msg::LaserScan output;
  void ToLaserscan(std::vector < PointData > src);
};

class LD00_LiPkg: public LiPkg
{
public:
  virtual void Transform(std::vector < PointData > & tmp);
};

class LD03_LiPkg: public LiPkg
{
public:
  virtual void Transform(std::vector < PointData > & tmp);
};

class LD08_LiPkg: public LiPkg
{
public:
  virtual void Transform(std::vector < PointData > & tmp);
};

class LD09_LiPkg: public LiPkg
{
public:
  virtual void Transform(std::vector < PointData > & tmp);
};

#endif  // LIPKG_H_
