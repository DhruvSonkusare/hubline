import path from "node:path";

import { expect, type Page, test } from "@playwright/test";

import { E2E_PASSWORD } from "../../playwright.config";

// Real PNGs for the Step 3 report, all at 1440x900.
const SHOTS = path.resolve(__dirname, "../../../reports/step-3-screenshots");
async function shot(page: Page, name: string) {
  // let toasts fade out so they don't cover the page in the report screenshots
  await expect(page.getByTestId("toast")).toHaveCount(0, { timeout: 10_000 });
  await page.screenshot({ path: path.join(SHOTS, `${name}.png`) });
}

async function login(page: Page, baseURL = "") {
  await page.goto(`${baseURL}/inbox`);
  await expect(page).toHaveURL(/\/login/); // the guard sends us to the login page
  await page.getByLabel("Password").fill(E2E_PASSWORD);
  await page.getByRole("button", { name: "Log in" }).click();
}

async function createDemoMessage(page: Page, scenario: string) {
  await page.getByRole("button", { name: "Choose a demo scenario" }).first().click();
  await page.getByRole("menuitem", { name: scenario, exact: true }).click();
  await expect(page.getByText("Demo message created").first()).toBeVisible();
}

async function waitForAgent(page: Page) {
  // AutoRefresh switches to 3 s while the agent works; wait until nothing is queued/working.
  await expect(page.getByTestId("feed")).toBeVisible();
  await expect(page.getByText(/Agent (queued|working)/)).toHaveCount(0, { timeout: 30_000 });
}

test.describe.serial("dashboard smoke: login -> new demo message -> approve", () => {
  test("full flow with screenshots", async ({ page }) => {
    // 1. Login (and a wrong password is rejected)
    await page.goto("/login");
    await expect(page.getByRole("heading", { name: "Hubline" })).toBeVisible();
    await shot(page, "01-login");
    await page.getByLabel("Password").fill("not-the-password");
    await page.getByRole("button", { name: "Log in" }).click();
    await expect(page.getByText("Incorrect password.")).toBeVisible();
    await login(page);
    await expect(page).toHaveURL(/\/inbox$/);

    // 2. Empty feed -> "No messages yet" with the New demo message button
    await expect(page.getByRole("heading", { name: "No messages yet" })).toBeVisible();
    await expect(page.getByRole("button", { name: "+ New demo message" })).toHaveCount(2);
    await shot(page, "02-feed-empty");

    // 3. New demo messages; the agent triages them on demand (no background loops)
    await createDemoMessage(page, "client-question");
    await createDemoMessage(page, "prod-incident");
    await createDemoMessage(page, "newsletter");
    await waitForAgent(page);
    const feed = page.getByTestId("feed");
    await expect(feed.getByRole("listitem")).toHaveCount(3);
    await expect(feed.getByText("P5")).toBeVisible(); // prod incident, priority by label + colour
    await page.mouse.move(0, 0);
    await shot(page, "03-feed");

    // 4. Detail: thread, agent summary, draft with open questions beside it
    await feed.getByRole("link", { name: /Rahul Verma/ }).click();
    await expect(page.getByRole("heading", { name: /webhook retry policy/i })).toBeVisible();
    await expect(page.getByText("Before sending, answer:")).toBeVisible();
    await expect(page.getByRole("checkbox")).toHaveCount(1);
    await expect(page.getByLabel("Draft reply text")).toHaveValue(/Hi Rahul/);
    await shot(page, "04-detail-draft-open-questions");

    // 5. Approve and send: the confirmation names the destination and lists unticked questions
    await page.getByLabel("Draft reply text").fill(
      "Hi Rahul,\n\nWe retry a failed webhook 5 times over 24 hours, with exponential backoff.\n\nBest,\nDhruv",
    );
    await page.getByRole("button", { name: "Approve and send" }).click();
    const dialog = page.getByRole("dialog", { name: "Send this reply?" });
    await expect(dialog).toBeVisible();
    await expect(dialog.getByTestId("unchecked-questions")).toContainText("1 open question is not ticked");
    await shot(page, "05-confirm-dialog");
    await dialog.getByRole("button", { name: "Send anyway" }).click();
    await expect(page.getByText("Sent via Demo data")).toBeVisible();
    await expect(page.getByText(/^Replied via Demo data/)).toBeVisible();
    await expect(page.getByRole("region", { name: "Thread" }).getByText("You", { exact: true })).toBeVisible();
    await page.mouse.move(0, 0);
    await shot(page, "06-detail-replied");

    // 6. Connectors page
    await page.getByRole("link", { name: "Connectors" }).click();
    await expect(page.getByRole("heading", { name: "Connectors" })).toBeVisible();
    await expect(page.getByRole("article", { name: "Demo data" })).toBeVisible();
    await shot(page, "07-connectors");
  });

  test("waking-up state while the backend is asleep", async ({ page }) => {
    const asleep = "http://localhost:3011";
    await login(page, asleep);
    await expect(page.getByText("Waking the backend…")).toBeVisible({ timeout: 20_000 });
    await expect(page.getByTestId("waking-elapsed")).toBeVisible();
    await page.waitForTimeout(2_500); // let the elapsed counter tick
    await shot(page, "08-waking");
  });
});
