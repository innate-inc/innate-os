import { useAuth0 } from "@auth0/auth0-react";
import styled from "styled-components";
import { STRIPE_PAYMENT_LINK } from "../../services/authService";
import { LogoutButton } from "./LogoutButton";
import innateLogo from "../../assets/innate.png";

const Container = styled.div`
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
  font-size: 28px;
  font-weight: bold;
  margin-bottom: 1rem;
  color: #ffffff; /* Bright white for title on dark background */
`;

const Subtitle = styled.p`
  font-size: 18px;
  margin-bottom: 1rem;
  max-width: 600px;
  color: #b0b0b0; /* Slightly dimmed text for readability */
  line-height: 1.5;
`;

const UserInfo = styled.div`
  padding: 1rem;
  border-radius: 8px;
  margin-bottom: 1rem;
  font-size: 16px;
  color: #e0e0e0;
  border: 1px solid #444; /* Subtle border instead of background */
`;

const ButtonContainer = styled.div`
  display: flex;
  gap: 1rem;
  margin-top: 1rem;
`;

const StripeButton = styled.a`
  padding: 12px 24px;
  background-color: #6772e5;
  color: white;
  border: none;
  border-radius: 6px;
  font-size: 16px;
  cursor: pointer;
  text-decoration: none;
  transition: background-color 0.2s ease;

  &:hover {
    background-color: #5469d4;
  }
`;

const ContactLink = styled.a`
  color: #6772e5; /* Stripe blue color for consistency */
  text-decoration: underline;
  margin-top: 1rem;

  &:hover {
    text-decoration: none;
  }
`;

export const UnauthorizedScreen = () => {
  const { user } = useAuth0();

  return (
    <Container>
      <LogoContainer>
        <Logo src={innateLogo} alt="Innate Robotics" />
      </LogoContainer>

      <Title>Access Restricted</Title>

      {user && user.email && (
        <UserInfo>
          You are signed in as <strong>{user.email}</strong>, but this account
          is not authorized.
        </UserInfo>
      )}

      <Subtitle>
        You can purchase access with a one-time payment or contact us directly.
      </Subtitle>

      <ButtonContainer>
        <StripeButton
          href={STRIPE_PAYMENT_LINK}
          target="_blank"
          rel="noopener noreferrer"
        >
          Get access now
        </StripeButton>
        <LogoutButton />
      </ButtonContainer>

      <ContactLink href="mailto:axel@innate.bot">
        Contact the founders for access
      </ContactLink>
    </Container>
  );
};
