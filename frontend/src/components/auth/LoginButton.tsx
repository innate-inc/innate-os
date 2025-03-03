import { useAuth0 } from "@auth0/auth0-react";
import styled from "styled-components";

const StyledButton = styled.button`
  padding: 10px 20px;
  background-color: #6772e5; /* Stripe blue for consistency */
  border: none;
  border-radius: ${({ theme }) => theme.borderRadius};
  color: #fff;
  font-size: 16px;
  font-family: ${({ theme }) => theme.fonts.body};
  font-weight: ${({ theme }) => theme.fontWeights.medium};
  cursor: pointer;
  transition: background-color 0.2s ease;

  &:hover {
    background-color: #5469d4;
  }
`;

export const LoginButton = () => {
  const { loginWithRedirect } = useAuth0();

  return (
    <StyledButton
      onClick={() =>
        loginWithRedirect({
          authorizationParams: {
            prompt: "login",
          },
        })
      }
    >
      Log In
    </StyledButton>
  );
};
