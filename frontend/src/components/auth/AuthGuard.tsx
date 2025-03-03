import { useAuth0 } from "@auth0/auth0-react";
import { ReactNode, useEffect } from "react";
import styled from "styled-components";
import { LoginButton } from "./LoginButton";
import { SignupButton } from "./SignupButton";
import { isAuthorized } from "../../services/authService";
import { UnauthorizedScreen } from "./UnauthorizedScreen";
import innateLogo from "../../assets/innate.png";

// Define a type for Auth0 errors which may include additional properties
interface Auth0Error extends Error {
  error?: string;
  error_description?: string;
}

interface AuthGuardProps {
  children: ReactNode;
}

const AuthContainer = styled.div`
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  height: 100vh;
  gap: 2rem;
  text-align: center;
  padding: 1rem;
  background-color: #121212; /* Dark background */
  color: #e0e0e0; /* Light text for dark background */
  position: relative;
`;

const LogoContainer = styled.div`
  position: absolute;
  top: 20px;
  left: 20px;
`;

const Logo = styled.img`
  height: 40px;
  width: auto;
`;

const Title = styled.h1`
  font-size: 24px;
  font-weight: bold;
  margin-bottom: 1rem;
  color: #ffffff; /* Bright white for title on dark background */
`;

const Subtitle = styled.p`
  font-size: 16px;
  margin-bottom: 2rem;
  max-width: 500px;
  color: #b0b0b0; /* Slightly dimmed text for readability */
`;

const ButtonContainer = styled.div`
  display: flex;
  gap: 1rem;
`;

const LoadingContainer = styled.div`
  display: flex;
  justify-content: center;
  align-items: center;
  height: 100vh;
  font-size: 18px;
`;

const ErrorMessage = styled.div`
  color: #ff6b6b; /* Brighter red for dark theme */
  margin-bottom: 1rem;
  padding: 0.5rem;
  border: 1px solid #ff6b6b;
  border-radius: 4px;
  background-color: rgba(
    255,
    107,
    107,
    0.1
  ); /* Semi-transparent red background */
  max-width: 500px;
`;

export const AuthGuard = ({ children }: AuthGuardProps) => {
  const { isAuthenticated, isLoading, error, loginWithRedirect, user } =
    useAuth0();

  useEffect(() => {
    // Log detailed auth state for debugging
    console.log("Detailed Auth state:", {
      isAuthenticated,
      isLoading,
      error: error
        ? {
            message: error.message,
            stack: error.stack,
            name: error.name,
            // Cast to Auth0Error to access additional properties
            error: (error as Auth0Error).error,
            error_description: (error as Auth0Error).error_description,
          }
        : null,
    });
  }, [isAuthenticated, isLoading, error]);

  if (isLoading) {
    return (
      <LoadingContainer>Loading authentication status...</LoadingContainer>
    );
  }

  if (error) {
    const auth0Error = error as Auth0Error;
    return (
      <AuthContainer>
        <LogoContainer>
          <Logo src={innateLogo} alt="Innate Robotics" />
        </LogoContainer>
        <Title>Authentication Error</Title>
        <ErrorMessage>
          {error.message ||
            "There was an error with the authentication process."}
          {auth0Error.error_description && (
            <div>Details: {auth0Error.error_description}</div>
          )}
        </ErrorMessage>
        <Subtitle>Please try logging in again.</Subtitle>
        <ButtonContainer>
          <button
            onClick={() =>
              loginWithRedirect({
                appState: { returnTo: window.location.pathname },
              })
            }
            style={{
              padding: "10px 20px",
              backgroundColor: "#6772e5" /* Stripe blue for consistency */,
              color: "white",
              border: "none",
              borderRadius: "4px",
              cursor: "pointer",
              fontSize: "16px",
            }}
          >
            Try Login Again
          </button>
        </ButtonContainer>
      </AuthContainer>
    );
  }

  if (!isAuthenticated) {
    return (
      <AuthContainer>
        <LogoContainer>
          <Logo src={innateLogo} alt="Innate Robotics" />
        </LogoContainer>
        <Title>Welcome to the First Open AI Operator for Robotics</Title>
        <Subtitle>
          Please log in with your existing account or sign up for a new account
          to access the robotics operator platform.
        </Subtitle>
        <ButtonContainer>
          <LoginButton />
          <SignupButton />
        </ButtonContainer>
      </AuthContainer>
    );
  }

  // Check if the authenticated user is authorized
  if (!isAuthorized(user)) {
    return <UnauthorizedScreen />;
  }

  // User is authenticated and authorized, render the children
  return <>{children}</>;
};
