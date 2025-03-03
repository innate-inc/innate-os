import { useAuth0 } from "@auth0/auth0-react";
import styled from "styled-components";

const StyledButton = styled.button`
  padding: 10px 20px;
  background-color: #38b2ac; /* Teal color for contrast with login button */
  border: none;
  border-radius: 6px;
  color: #fff;
  font-size: 16px;
  cursor: pointer;
  transition: background-color 0.2s ease;

  &:hover {
    background-color: #2c9a94;
  }
`;

export const SignupButton = () => {
  const { loginWithRedirect } = useAuth0();

  return (
    <StyledButton
      onClick={() =>
        loginWithRedirect({
          authorizationParams: {
            screen_hint: "signup",
          },
        })
      }
    >
      Sign Up
    </StyledButton>
  );
};
