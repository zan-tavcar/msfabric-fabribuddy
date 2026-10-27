/**
 * Paddle's overlay checkout. Paddle.js is loaded only when someone chooses a
 * plan (the CSP allows cdn.paddle.com only when Paddle is the provider), and
 * the checkout opens on a transaction the server created — the price and the
 * organisation are never stated by the browser.
 */

interface PaddleCheckout {
  transactionId: string;
  clientToken: string;
  environment: string;
  email?: string;
}

interface PaddleGlobal {
  Environment: { set: (env: string) => void };
  Initialize: (options: { token: string; eventCallback?: (e: { name?: string }) => void }) => void;
  Checkout: {
    open: (options: {
      transactionId: string;
      customer?: { email: string };
      settings?: { displayMode?: string; successUrl?: string; theme?: string };
    }) => void;
  };
}

declare global {
  interface Window {
    Paddle?: PaddleGlobal;
  }
}

let loading: Promise<PaddleGlobal> | null = null;
let initialisedWith = "";

function load(): Promise<PaddleGlobal> {
  if (window.Paddle) return Promise.resolve(window.Paddle);
  if (!loading) {
    loading = new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = "https://cdn.paddle.com/paddle/v2/paddle.js";
      script.async = true;
      script.onload = () =>
        window.Paddle ? resolve(window.Paddle) : reject(new Error("Paddle did not load."));
      script.onerror = () => {
        loading = null;
        reject(new Error("Could not load the checkout. Check your connection and try again."));
      };
      document.head.appendChild(script);
    });
  }
  return loading;
}

/** Open the overlay; `onCompleted` fires when Paddle reports the payment done. */
export async function openPaddleCheckout(
  checkout: PaddleCheckout,
  onCompleted: () => void,
): Promise<void> {
  const Paddle = await load();
  if (initialisedWith !== checkout.clientToken) {
    if (checkout.environment !== "production") Paddle.Environment.set("sandbox");
    Paddle.Initialize({
      token: checkout.clientToken,
      eventCallback: (event) => {
        if (event.name === "checkout.completed") onCompleted();
      },
    });
    initialisedWith = checkout.clientToken;
  }
  Paddle.Checkout.open({
    transactionId: checkout.transactionId,
    customer: checkout.email ? { email: checkout.email } : undefined,
    settings: {
      displayMode: "overlay",
      theme: "light",
      successUrl: `${window.location.origin}/?billing=success`,
    },
  });
}
