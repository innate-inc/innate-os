export interface Message {
  text: string;
  sender: "user" | "robot" | "robot_thoughts" | "robot_anticipation";
  timestamp: number;
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
  let i = 0;
  while (i < messages.length) {
    const msg = messages[i];
    if (msg.sender === "user" || msg.sender === "robot") {
      grouped.push(msg);
      i++;
    } else if (
      msg.sender === "robot_thoughts" ||
      msg.sender === "robot_anticipation"
    ) {
      const extras: { text: string; timestamp: number }[] = [];
      const groupStartTimestamp = msg.timestamp;
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
      const prev = grouped.length > 0 ? grouped[grouped.length - 1] : null;
      let durationSeconds = 0;
      if (prev && prev.sender === "robot") {
        const lastExtraTimestamp = extras[extras.length - 1].timestamp;
        durationSeconds = Math.ceil(
          (lastExtraTimestamp - prev.timestamp) / 1000
        );
      }
      grouped.push({
        sender: "robot_grouped",
        groupedExtras: extras.map((e) => e.text),
        durationSeconds,
        timestamp: groupStartTimestamp,
      });
    } else {
      grouped.push(msg);
      i++;
    }
  }
  return grouped;
}
