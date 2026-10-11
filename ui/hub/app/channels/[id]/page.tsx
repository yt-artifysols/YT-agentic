import { CHANNEL_IDS, DEMO } from "@/lib/demo";
import { ChannelView } from "./ChannelView";

export function generateStaticParams() {
  return DEMO ? CHANNEL_IDS.map((id) => ({ id })) : [];
}

export default function ChannelPage() {
  return <ChannelView />;
}
