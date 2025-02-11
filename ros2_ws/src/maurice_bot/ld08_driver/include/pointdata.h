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

#ifndef POINTDATA_H_
#define POINTDATA_H_

#include <stdint.h>
#include <vector>
#include <iostream>

struct PointData
{
  // Polar coordinate representation
  float angle;
  uint16_t distance;
  uint8_t confidence;

  // Rectangular coordinate representation
  double x;
  double y;

  PointData(float angle, uint16_t distance, uint8_t confidence, double x = 0, double y = 0)
  {
    this->angle = angle;
    this->distance = distance;
    this->confidence = confidence;
    this->x = x;
    this->y = y;
  }

  PointData() {
  }

  friend std::ostream & operator << (std::ostream & os, const PointData & data)
      {
      os << data.angle << " " \
      << data.distance << " " \
      << (int)data.confidence << " " \
      << data.x << " " \
      << data.y;
      return os;
    }
};


typedef std::vector < PointData > Points2D;

#endif  // POINTDATA_H_
