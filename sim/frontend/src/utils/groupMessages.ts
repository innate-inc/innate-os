export interface Message {
  text: string;
  sender:
    | "user"
    | "robot"
    | "robot_thoughts"
    | "robot_anticipation"
    | "system"
    | "vision_agent_output";
  timestamp: number;
  isError?: boolean;
}

export interface GroupedMessage {
  sender: "robot_grouped";
  groupedExtras: string[];
  durationSeconds: number;
  timestamp: number;
}

export type DisplayMessage = Message | GroupedMessage;

/**
 * Groups contiguous 'robot_thoughts' or 'robot_anticipation' messages
 * into a single GroupedMessage object.
 */
export function groupMessages(messages: Message[]): DisplayMessage[] {
  const grouped: DisplayMessage[] = [];

  // These variables track the last seen message timestamp as we iterate.
  let lastMessageTimestamp: number | undefined;

  let i = 0;
  while (i < messages.length) {
    const msg = messages[i];

    // For direct user messages, add and update the timestamp.
    if (msg.sender === "user") {
      grouped.push(msg);
      lastMessageTimestamp = msg.timestamp;
      i++;

      // For direct robot messages, add and update the timestamp.
    } else if (msg.sender === "robot") {
      grouped.push(msg);
      lastMessageTimestamp = msg.timestamp;
      i++;

      // For system messages, add and update the timestamp.
    } else if (msg.sender === "system") {
      grouped.push(msg);
      lastMessageTimestamp = msg.timestamp;
      i++;

      // For robot_thoughts or robot_anticipation we group them.
    } else if (
      msg.sender === "robot_thoughts" ||
      msg.sender === "robot_anticipation"
    ) {
      const displayedGroupedTexts: string[] = [];
      const groupStartTimestamp = msg.timestamp;
      let latestTimestampInThisGroup = msg.timestamp; // Tracks the actual last timestamp for duration

      let lastSeenRobotThoughtsText: string | null = null;
      let lastSeenRobotAnticipationText: string | null = null;

      const initialIndexOfGroup = i; // To check if any messages were processed for this group

      // Collect contiguous robot_thoughts or robot_anticipation messages
      while (
        i < messages.length &&
        (messages[i].sender === "robot_thoughts" ||
          messages[i].sender === "robot_anticipation")
      ) {
        const currentProcessingMessage = messages[i];
        latestTimestampInThisGroup = currentProcessingMessage.timestamp; // Always update with the current message's timestamp

        let addTextToDisplay = true;

        if (currentProcessingMessage.sender === "robot_thoughts") {
          if (currentProcessingMessage.text === lastSeenRobotThoughtsText) {
            addTextToDisplay = false;
          }
          // Always update lastSeenRobotThoughtsText with the current message's text for the next comparison
          lastSeenRobotThoughtsText = currentProcessingMessage.text;
        } else if (currentProcessingMessage.sender === "robot_anticipation") {
          if (currentProcessingMessage.text === lastSeenRobotAnticipationText) {
            addTextToDisplay = false;
          }
          // Always update lastSeenRobotAnticipationText with the current message's text for the next comparison
          lastSeenRobotAnticipationText = currentProcessingMessage.text;
        }

        if (addTextToDisplay) {
          displayedGroupedTexts.push(currentProcessingMessage.text);
        }

        i++; // Move to the next message
      }

      // Calculate duration based on the actual latest timestamp in the group
      // and the timestamp of the message before this group started.
      let durationSeconds = 0;
      if (lastMessageTimestamp !== undefined && initialIndexOfGroup < i) {
        // Ensure group had messages
        durationSeconds = Math.ceil(
          latestTimestampInThisGroup - lastMessageTimestamp
        );
      }

      // Only add the group if there are texts to display or if you always want to show a group placeholder
      // For now, we add if there are texts. If no texts were added (all duplicates), this group won't appear.
      // This might need adjustment based on whether an empty group (with duration) is desired.
      if (displayedGroupedTexts.length > 0) {
        grouped.push({
          sender: "robot_grouped",
          groupedExtras: displayedGroupedTexts,
          durationSeconds,
          timestamp: groupStartTimestamp, // Timestamp of the first message that started this group
        });
      }
    } else {
      // Any other message type is handled directly.
      grouped.push(msg);
      i++;
    }
  }

  return grouped;
}
