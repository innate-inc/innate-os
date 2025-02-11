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

#include <math.h>
#include <iostream>
#include <algorithm>
#include "../include/lipkg.h"
#include "../include/slbf.h"

/*
  \brief      Set current speed
  \param[in]
    \arg  speed           Current lidar speed
    \arg  strict_policy   The flag to enable very strict filtering
  \param[out] none
  \retval     none
*/
Slbf::Slbf(int speed, bool strict_policy)
{
  curr_speed = speed;
  enable_strict_policy = strict_policy;
}

Slbf::~Slbf()
{
}

/*
  \brief        Filter within 1m to filter out unreasonable data points
  \param[in]
    \arg data   A circle of lidar data packed
  \param[out]   none
  \retval       Standard data
*/
Points2D Slbf::NearFilter(const Points2D & data) const
{
  Points2D normal, pending, item;
  std::vector<Points2D> group;
  int sunshine_amount = 0;

  for (auto n : data) {
    if (n.distance < 1000) {
      pending.push_back(n);
    } else {
      normal.push_back(n);
    }
  }

  if (data.empty()) {
    return normal;
  }

  double angle_delta_up_limit = curr_speed / SCAN_FRE * 1.5;
  double angle_delta_down_limit = curr_speed / SCAN_FRE - 0.2;

  std::sort(
    pending.begin(), pending.end(), [](PointData a, PointData b) {return a.angle < b.angle;}
  );

  PointData last(-10, 0, 0);

  for (auto n : pending) {
    if (abs(n.angle - last.angle) > angle_delta_up_limit ||
      abs(n.distance - last.distance) > 50)
    {
      if (item.empty() == false) {
        group.push_back(item);
        item.clear();
      }
    }
    item.push_back(n);
    last = n;
  }

  if (item.empty() == false) {
    group.push_back(item);
  }

  if (group.empty()) {
    return normal;
  }


  auto first_item = group.front().front();
  auto last_item = group.back().back();
  if (abs(first_item.angle + 360.f - last_item.angle) < angle_delta_up_limit &&
    abs(first_item.distance - last_item.distance) < 50)
  {
    if (group.size() > 1) {
      group.front().insert(group.front().begin(), group.back().begin(), group.back().end());
      group.erase(group.end() - 1);
    }
  }

  for (auto n : group) {
    if (n.size() == 0) {
      continue;
    }

    if (n.size() > 15) {
      normal.insert(normal.end(), n.begin(), n.end());
      continue;
    }


    for (auto m : n) {
      int flag = m.confidence & 0x01;
      sunshine_amount += (flag == 1);
    }

    double sunshine_rate = static_cast<double>(sunshine_amount) / static_cast<double>(n.size());

    double confidence_avg = 0;
    double dis_avg = 0;
    for (auto m : n) {
      confidence_avg += m.confidence;
      dis_avg += m.distance;
    }
    confidence_avg /= n.size();
    dis_avg /= n.size();

    if (sunshine_rate < 0.2 && confidence_avg > CONFIDENCE_LOW) {
      normal.insert(normal.end(), n.begin(), n.end());
      continue;
    }

    if (sunshine_rate > 0.5 && confidence_avg < CONFIDENCE_LOW) {
      continue;
    }

    if (enable_strict_policy) {
      if (dis_avg < 300 && confidence_avg < CONFIDENCE_LOW && n.size() < 5) {
        continue;
      }

      if (dis_avg < 300 && sunshine_rate > 0.9 && n.size() < 3) {
        continue;
      }
    }

    double diff_avg = 0;

    for (uint32_t i = 1; i < (uint32_t)n.size(); i++) {
      diff_avg += abs(n[i].angle - n[i - 1].angle);
    }

    diff_avg /= static_cast<double>(n.size() - 1);

    if (diff_avg > angle_delta_down_limit) {
      normal.insert(normal.end(), n.begin(), n.end());
    }
  }

  return normal;
}

/*
  \brief           Enable strong filtering
  \param[in]
    \arg  enable : true ，false
  \param[out] none
  \retval
*/
void Slbf::EnableStrictPolicy(bool enable)
{
  enable_strict_policy = enable;
}
