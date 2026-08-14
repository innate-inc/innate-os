// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
#pragma once

#include <algorithm>
#include <cctype>
#include <string>

/**
 * Sanitize hostname to follow DNS rules:
 * - Only letters (a-z), numbers (0-9), and hyphens (-)
 * - Cannot start or end with hyphens
 * - Max 63 characters
 *
 * mars_control is the only writer of the robot's hostname, so the provisioning laptop
 * derives the .local name it waits on by mirroring exactly this function.
 */
inline std::string sanitize_hostname(const std::string& hostname) {
    if (hostname.empty())
        return "mars";

    std::string result;
    result.reserve(hostname.length());

    // Convert to lowercase and replace invalid chars with hyphens
    for (char c : hostname) {
        if (std::isalnum(static_cast<unsigned char>(c))) {
            result += static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
        } else {
            result += '-';
        }
    }

    // Remove consecutive hyphens
    result.erase(std::unique(result.begin(), result.end(), [](char a, char b) { return a == '-' && b == '-'; }),
                 result.end());

    // Trim leading/trailing hyphens
    size_t start = result.find_first_not_of('-');
    size_t end = result.find_last_not_of('-');
    if (start == std::string::npos)
        return "mars";

    result = result.substr(start, std::min(end - start + 1, size_t(63)));
    if (result.back() == '-')
        result.pop_back();

    return result.empty() ? "mars" : result;
}
