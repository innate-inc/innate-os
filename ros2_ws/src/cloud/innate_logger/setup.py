from setuptools import setup

package_name = "innate_logger"

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
        ("share/" + package_name + "/launch", ["launch/logger.launch.py"]),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="Innate Engineering",
    maintainer_email="eng@innate.bot",
    description="ROS 2 node for logging robot vitals, directives, and chat to the cloud.",
    license="Proprietary",
    entry_points={
        "console_scripts": [
            "logger_node = innate_logger.logger_node:main",
        ],
    },
)
