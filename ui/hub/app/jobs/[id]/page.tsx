import { DEMO, DEMO_JOB_IDS } from "@/lib/demo";
import { JobView } from "./JobView";

export function generateStaticParams() {
  return DEMO ? DEMO_JOB_IDS.map((id) => ({ id })) : [];
}

export default function JobPage() {
  return <JobView />;
}
