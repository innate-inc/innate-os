from setuptools import setup

package_name = "innate_uninavid"

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
    ],
    install_requires=["setuptools", "httpx>=0.24", "httpx-ws>=0.6", "python-dotenv"],
    zip_safe=True,
    maintainer="Innate Engineering",
    maintainer_email="eng@innate.bot",
    description="ROS 2 node bridging compressed camera images to a cloud websocket and publishing cmd_vel.",
    license="Proprietary",
    entry_points={
        "console_scripts": [
            "uninavid_node = innate_uninavid.node:main",
        ],
    },
)
