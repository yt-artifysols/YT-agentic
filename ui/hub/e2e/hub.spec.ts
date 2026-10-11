// End-to-end: an approver approves, edits and rejects videos in the UI with a passkey (Chrome
// virtual authenticator); an operator uses a kill switch. e2e/verify.py then checks the
// tokens, approvals rows, workflow outcomes and audit events in Postgres.
import { expect, test, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const stack = JSON.parse(readFileSync(join(__dirname, ".stack.json"), "utf8")) as {
  channel: string;
  jobs: { approve: string; edit: string; reject: string };
  invites: { approver: string; operator: string };
};

async function addPasskeyDevice(page: Page): Promise<void> {
  const cdp = await page.context().newCDPSession(page);
  await cdp.send("WebAuthn.enable");
  await cdp.send("WebAuthn.addVirtualAuthenticator", {
    options: {
      protocol: "ctap2",
      transport: "internal",
      hasResidentKey: true,
      hasUserVerification: true,
      isUserVerified: true,
      automaticPresenceSimulation: true,
    },
  });
}

async function register(page: Page, invite: string): Promise<void> {
  await page.goto(`/login?invite=${encodeURIComponent(invite)}`);
  await page.getByRole("button", { name: "Register passkey" }).click();
  await expect(page).toHaveURL("/");
}

async function openJob(page: Page, jobId: string): Promise<void> {
  await page.goto(`/jobs/${jobId}`);
  await expect(page.getByTestId("job-title")).toBeVisible();
}

test("approver approves, edits and rejects; operator uses a kill switch", async ({ browser }) => {
  // ----------------------------------------------------------------- approver
  const approverCtx = await browser.newContext();
  const page = await approverCtx.newPage();
  await addPasskeyDevice(page);
  await register(page, stack.invites.approver);
  await expect(page.getByTestId("whoami")).toContainText("approver");
  await page.goto("/review");
  await expect(page.getByTestId("queue-row")).toHaveCount(3);

  // Approving needs the "I reviewed this version" confirmation; there is no one-key approve.
  await openJob(page, stack.jobs.approve);
  await page.getByRole("tab", { name: /Checks/ }).click();
  await expect(page.getByTestId("sources")).toBeVisible();
  await expect(page.getByTestId("approve")).toBeDisabled();
  await page.keyboard.press("a");
  await expect(page).not.toHaveURL(/done=approved/);
  await page.getByTestId("reviewed").check();
  await page.getByTestId("approve").click();
  await expect(page).toHaveURL(/done=approved/);

  // Edit: never approves; the job goes back to QA and returns with a diff.
  await openJob(page, stack.jobs.edit);
  await page.getByTestId("edit").click();
  await page.locator('input[name="title"]').fill("Edited in the hub");
  await page.getByTestId("save-edit").click();
  await expect(page).toHaveURL(/done=edited/);
  await expect
    .poll(
      async () => {
        await openJob(page, stack.jobs.edit);
        return (await page.getByTestId("approve").count()) > 0;
      },
      { timeout: 30_000 },
    )
    .toBe(true);
  await expect(page.getByTestId("job-title")).toHaveText("Edited in the hub");
  await expect(page.getByTestId("diff-title")).toContainText("+ Edited in the hub");
  await page.getByTestId("reviewed").check();
  await page.getByTestId("approve").click();
  await expect(page).toHaveURL(/done=approved/);

  // Reject: needs a reason and a rework stage.
  await openJob(page, stack.jobs.reject);
  await expect(page.getByTestId("approve")).toBeVisible();
  await page.keyboard.press("r");
  await page.locator('textarea[name="reason"]').fill("Off-brand tone and an unsupported claim");
  await page.locator('select[name="rework_stage"]').selectOption("discard");
  await page.getByTestId("confirm-reject").click();
  await expect(page).toHaveURL(/done=rejected/);

  // Sign out, then sign back in with the same passkey.
  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page).toHaveURL("/login");
  await page.locator('input[name="username"]').fill("e2e-approver");
  await page.getByRole("button", { name: "Sign in with passkey" }).click();
  await expect(page).toHaveURL("/");
  await expect(page.getByTestId("whoami")).toContainText("approver");
  // Approvers have no kill-switch controls.
  await expect(page.getByRole("link", { name: "Kill switches" })).toHaveCount(0);
  await approverCtx.close();

  // ----------------------------------------------------------------- operator
  const opCtx = await browser.newContext();
  const op = await opCtx.newPage();
  await addPasskeyDevice(op);
  await register(op, stack.invites.operator);
  await op.getByRole("link", { name: "Kill switches" }).click();
  await op.getByPlaceholder("e.g. strike warning on channel").fill("e2e drill");
  await op.getByTestId(`toggle-channel-${stack.channel}`).click();
  await op.getByRole("dialog").getByRole("button", { name: "Pause" }).click();
  await expect(op.locator(".toast").last()).toContainText("paused");
  await op.getByTestId(`toggle-channel-${stack.channel}`).click();
  await expect(op.locator(".toast").last()).toContainText("resumed");
  await opCtx.close();
});
