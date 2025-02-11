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

#ifndef SLBF_H_
#define SLBF_H_

#include <stdint.h>
#include <vector>
#include "../include/pointdata.h"

class Slbf
{
private:
  const int CONFIDENCE_LOW = 92;

  // Default scanning frequency, which can be changed according to radar protocol
  const int SCAN_FRE = 2300;
  double curr_speed;

  // whether strict filtering is enabled within 300 mm,
  // the effective value may be lost,
  // and the time sequence of recharging needs to be disabled
  bool enable_strict_policy;
  Slbf() = delete;
  Slbf(const Slbf &) = delete;
  Slbf & operator = (const Slbf &) = delete;

public:
  explicit Slbf(int speed, bool strict_policy = true);
  Points2D NearFilter(const Points2D & tmp) const;
  void EnableStrictPolicy(bool enable);
  ~Slbf();
};

#endif  // SLBF_H_
