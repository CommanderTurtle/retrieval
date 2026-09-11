import { homedir } from "node:os";
import { join } from "node:path";
import type { ExtensionAPI } from "@oh-my-pi/pi-coding-agent";

export default function retrievalSessionClose(pi: ExtensionAPI) {
	const dispatch = () => {
		const root = process.env.RETRIEVAL_ROOT || join(homedir(), "Hermes", "retrieval");
		const script = join(root, "hooks", "session-close.sh");
		const child = Bun.spawn(["bash", script, "omp"], {
			cwd: homedir(),
			env: {
				...process.env,
				PI_CODING_AGENT_DIR:
					process.env.PI_CODING_AGENT_DIR || join(homedir(), ".omp", "agent"),
			},
			stdin: "ignore",
			stdout: "ignore",
			stderr: "ignore",
		});
		child.unref();
	};

	// A /new, /resume, or /fork replaces the active session without exiting the
	// process. Reconcile before that switch as well as during final teardown.
	pi.on("session_before_switch", async () => dispatch());
	pi.on("session_shutdown", async () => dispatch());
}
