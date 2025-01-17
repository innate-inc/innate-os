#!/bin/bash

# Set the Discovery Server IP and port
DISCOVERY_SERVER_IP="127.0.0.1"  # Change this to your Discovery Server IP
DISCOVERY_SERVER_PORT="11811"    # Change this to your Discovery Server port

# Set participant ID
PARTICIPANT_ID="1"

# Create XML configuration for Discovery Server
cat > discovery_config.xml << EOF
<?xml version="1.0" encoding="UTF-8" ?>
<dds>
    <profiles xmlns="http://www.eprosima.com/XMLSchemas/fastRTPS_Profiles">
        <participant profile_name="discovery_check_profile">
            <rtps>
                <builtin>
                    <discovery_config>
                        <discoveryProtocol>SERVER</discoveryProtocol>
                    </discovery_config>
                    <metatrafficUnicastLocatorList>
                        <locator>
                            <udpv4>
                                <address>${DISCOVERY_SERVER_IP}</address>
                                <port>${DISCOVERY_SERVER_PORT}</port>
                            </udpv4>
                        </locator>
                    </metatrafficUnicastLocatorList>
                </builtin>
                <propertiesPolicy>
                    <properties>
                        <property>
                            <name>dds.discovery.server.guidPrefix</name>
                            <value>44.53.00.5f.45.50.52.4f.53.49.4d.41</value>
                        </property>
                    </properties>
                </propertiesPolicy>
            </rtps>
        </participant>
    </profiles>
</dds>
EOF

# Run FastDDS Discovery Tool with configuration
fastdds discovery -i ${PARTICIPANT_ID} -x discovery_config.xml

# Check the exit status
if [ $? -eq 0 ]; then
    echo "Successfully connected to Discovery Server at ${DISCOVERY_SERVER_IP}:${DISCOVERY_SERVER_PORT}"
else
    echo "Failed to connect to Discovery Server at ${DISCOVERY_SERVER_IP}:${DISCOVERY_SERVER_PORT}"
fi

# Clean up configuration file
rm discovery_config.xml