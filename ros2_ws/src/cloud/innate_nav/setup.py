# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from setuptools import setup

package_name = "innate_nav"

setup(
    name=package_name,
    version="0.1.0",
    packages=[package_name],
    data_files=[
        (
            "share/ament_index/resource_index/packages",
            ["resource/" + package_name],
        ),
        ("share/" + package_name, ["package.xml"]),
        ("share/" + package_name + "/launch", ["launch/innate_nav.launch.py"]),
        ("share/" + package_name + "/config", ["config/params.yaml"]),
    ],
    install_requires=["setuptools", "websockets", "numpy"],
    zip_safe=True,
    maintainer="Innate Engineering",
    maintainer_email="eng@innate.bot",
    description="ROS 2 node that drives the base from the Innate navigation policy service: streams the nav camera to it and tracks the waypoint plans it returns.",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "innate_nav_node = innate_nav.node:main",
            "wide_view = innate_nav.wide_view:main",
        ],
    },
)
