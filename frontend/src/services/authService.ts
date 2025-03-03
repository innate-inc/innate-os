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
