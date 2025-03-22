from setuptools import setup, find_packages

setup(
    name="maurice_control_simple",
    version="0.1.0",
    packages=find_packages(),
    install_requires=[
        "numpy",
        "dynamixel-sdk",
    ],
    author="Maurice Team",
    author_email="info@maurice.ai",
    description="A simple Python package to control Maurice robot without ROS",
    keywords="robotics, control, maurice",
    python_requires=">=3.6",
)
