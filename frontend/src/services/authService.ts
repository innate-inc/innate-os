import { User } from "@auth0/auth0-react";

// List of authorized users (emails)
const AUTHORIZED_USERS = [
  "axel@innate.bot", // Axel's email
];

// Domain whitelist - any email with these domains is authorized
const AUTHORIZED_DOMAINS = [
  "innate.bot", // All Innate employees
];

// Stripe payment link for unauthorized users
export const STRIPE_PAYMENT_LINK = "https://buy.stripe.com/00g4gp5wA8MDdi07st";

/**
 * Check if a user is authorized to access the application
 * @param user The Auth0 user object
 * @returns True if the user is authorized, false otherwise
 */
export const isAuthorized = (user: User | undefined): boolean => {
  if (!user || !user.email) {
    return false;
  }

  const email = user.email.toLowerCase();

  // Check if the email is in the authorized users list
  if (AUTHORIZED_USERS.includes(email)) {
    return true;
  }

  // Check if the email domain is in the authorized domains list
  for (const domain of AUTHORIZED_DOMAINS) {
    if (email.endsWith(`@${domain}`)) {
      return true;
    }
  }

  return false;
};

/**
 * Fetch user info from the backend and store it
 * This ensures the backend has the user's email for authorization
 * @param user The Auth0 user object
 * @param accessToken The access token from Auth0
 * @returns The user info from the backend
 */
export const fetchAndStoreUserInfo = async (
  user: User | undefined,
  accessToken: string
): Promise<{
  user_id: string;
  email: string;
  is_authorized: boolean;
} | null> => {
  if (!user || !accessToken) {
    console.log("No user or access token to fetch info for");
    return null;
  }

  console.log("User email from Auth0:", user.email);
  console.log("User sub from Auth0:", user.sub);

  try {
    // Get the API base URL from environment variables
    const apiBaseUrl = import.meta.env.VITE_SIM_BASE_URL || "";
    const endpoint = `${apiBaseUrl}/auth/user-info`;

    console.log(`Calling endpoint: ${endpoint}`);

    // Call the backend API to store user info
    const response = await fetch(endpoint, {
      method: "GET",
      headers: {
        Authorization: `Bearer ${accessToken}`,
        "Content-Type": "application/json",
      },
    });

    console.log("Response status:", response.status);

    if (!response.ok) {
      throw new Error(`Failed to store user info: ${response.statusText}`);
    }

    const data = await response.json();
    console.log("User info stored successfully:", data);
    return data;
  } catch (error) {
    console.error("Error storing user info:", error);
    return null;
  }
};
