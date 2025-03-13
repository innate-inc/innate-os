export interface Message {
  text: string;
  sender: "user" | "robot" | "robot_thoughts" | "robot_anticipation" | "system";
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
      const extras: { text: string; timestamp: number }[] = [];
      const groupStartTimestamp = msg.timestamp;

      // Collect contiguous robot_thoughts or robot_anticipation messages.
      while (
        i < messages.length &&
        (messages[i].sender === "robot_thoughts" ||
          messages[i].sender === "robot_anticipation")
      ) {
        extras.push({
          text: messages[i].text,
          timestamp: messages[i].timestamp,
        });
        i++;
      }

      // Calculate duration based on the last message timestamp
      let durationSeconds = 0;
      if (lastMessageTimestamp !== undefined && extras.length > 0) {
        const lastExtraTimestamp = extras[extras.length - 1].timestamp;
        durationSeconds = Math.ceil(lastExtraTimestamp - lastMessageTimestamp);
      }

      grouped.push({
        sender: "robot_grouped",
        groupedExtras: extras.map((e) => e.text),
        durationSeconds,
        timestamp: groupStartTimestamp,
      });
    } else {
      // Any other message type is handled directly.
      grouped.push(msg);
      i++;
    }
  }

  return grouped;
}
