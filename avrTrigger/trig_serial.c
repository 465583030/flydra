/* 

Program to control green lights and trigger external (Photron) cameras
under control serial port (for use with flydra).

DDB0 - trigger source output to cameras

Serial port parameters: 4800 baud, 8N1.

*/

#include <avr/io.h>
#include <avr/signal.h>
#include <avr/interrupt.h>

#define FALSE 0
#define TRUE 1
void OSCCAL_calibration(void); /* forward decl. */
void Delay(unsigned int millisec); /* forward decl. */

#define ACTION_NONE 0
#define ACTION_GO   'g'
#define ACTION_STOP 's'

void USART_init(void) {
  unsigned int ubrr;

  // Set baud rate
  ubrr = 12; // 4800 bps, see atmega169 manual pg. 174
  UBRRH = (unsigned char)(ubrr>>8);
  UBRRL = (unsigned char)ubrr;

  // Enable receiver and transmitter
  UCSRB = (1<<RXEN) | (1<<TXEN);
  
  // Set frame format 8N1
  // UCSRC defaults to 8N1 = (3<<UCSZ0)

  // Set frame format 7E1
  //UCSRC = (1<<UPM1)|(1<<UCSZ1);
}


void Initialization(void) {
  OSCCAL_calibration();

  // USART
  USART_init();

  DDRB = (1<<DDB0); // output pin enabled
  PORTB |= (1<<DDB0); // set high

  // timer2
  ASSR = (1<<AS2);        //select asynchronous operation of timer2 (32,768kHz)
  TIMSK2=(1<<OCIE2A); // enable interrupt
  TCCR2A = 0; //stop timer2

  OCR2A = 161;    // set timer2 compare value, calibrated on 2006-07-28 to give 10.0 msec period

  TCNT2 = 0;                      
  // clear timer2 counter, (do
  // after setting ocr2a because
  // compare is blocked for one
  // clock after setting)

  TCCR2A = (1<<CS20);     // start timer2 with no prescaling
}

SIGNAL(SIG_OUTPUT_COMPARE2)
{
  if (PORTB && (1<<DDB0) ) {
    // value was high, set low
    PORTB &= ~(1<<DDB0); // turn off trigger pin
  } else {
    // value was low, set high
    PORTB |= (1<<DDB0); // turn on trigger pin
  }
  TCNT2 = 0;                      // clear timer2 counter
}

int main(void) {
  unsigned char last_char_input;
  unsigned char do_action;

  Initialization();

  while(1) {
    
    while (!(UCSRA & (1<<RXC))) {} // wait until USART received byte
    last_char_input = UDR; // get byte
    do_action = ACTION_NONE;
    
    switch (last_char_input) {
    case 'g':
      do_action = ACTION_GO;
      break;
    case 's':
      do_action = ACTION_STOP;
      break;
    default:
      last_char_input = '?'; // echo back '?'
      break;
    }

    switch (do_action) {

    case ACTION_GO:
      TCCR2A = (1<<CS20);     // start timer2 (with no prescaling)
      break;

    case ACTION_STOP:
      TCCR2A = 0;             // stop  timer2 (with no prescaling)
      PORTB &= ~(1<<DDB0); // turn off trigger pin
      break;

    case ACTION_NONE:
    default:
      break;
      
    }
      
    UDR=last_char_input; // echo (transmit) back on USART
  }
}

/*****************************************************************************
*
*   Function name : OSCCAL_calibration
*
*   Returns :       None
*
*   Parameters :    None
*
*   Purpose :       Calibrate the internal OSCCAL byte, using the external 
*                   32,768 kHz crystal as reference
*
*****************************************************************************/
void OSCCAL_calibration(void)
{
    unsigned char calibrate = FALSE;
    int temp;
    unsigned char tempL;

    CLKPR = (1<<CLKPCE);        // set Clock Prescaler Change Enable

    CLKPR = (1<<CLKPS1) | (1<<CLKPS0); // ADS - sets clock division factor to 8
    // ADS - thus, with calibrated internal crystal at * MHz, this gives us 1 MHz
    
    TIMSK2 = 0;             //disable OCIE2A and TOIE2

    ASSR = (1<<AS2);        //select asynchronous operation of timer2 (32,768kHz)
    
    OCR2A = 200;            // set timer2 compare value 

    TIMSK0 = 0;             // delete any interrupt sources
        
    TCCR1B = (1<<CS10);     // start timer1 with no prescaling
    TCCR2A = (1<<CS20);     // start timer2 with no prescaling

    while((ASSR & 0x01) | (ASSR & 0x04));       //wait for TCN2UB and TCR2UB to be cleared

    Delay(1000);    // wait for external crystal to stabilise
    
    while(!calibrate)
    {
        cli(); // mt __disable_interrupt();  // disable global interrupt
        
        TIFR1 = 0xFF;   // delete TIFR1 flags
        TIFR2 = 0xFF;   // delete TIFR2 flags
        
        TCNT1H = 0;     // clear timer1 counter
        TCNT1L = 0;
        TCNT2 = 0;      // clear timer2 counter
           
        // shc/mt while ( !(TIFR2 && (1<<OCF2A)) );   // wait for timer2 compareflag    
        while ( !(TIFR2 & (1<<OCF2A)) );   // wait for timer2 compareflag

        TCCR1B = 0; // stop timer1

        sei(); // __enable_interrupt();  // enable global interrupt
    
        // shc/mt if ( (TIFR1 && (1<<TOV1)) )
        if ( (TIFR1 & (1<<TOV1)) )
        {
            temp = 0xFFFF;      // if timer1 overflows, set the temp to 0xFFFF
        }
        else
        {   // read out the timer1 counter value
            tempL = TCNT1L;
            temp = TCNT1H;
            temp = (temp << 8);
            temp += tempL;
        }
    
        if (temp > 6250)
        {
            OSCCAL--;   // the internRC oscillator runs to fast, decrease the OSCCAL
        }
        else if (temp < 6120)
        {
            OSCCAL++;   // the internRC oscillator runs to slow, increase the OSCCAL
        }
        else
            calibrate = TRUE;   // the interRC is correct

        TCCR1B = (1<<CS10); // start timer1
    }
}

/*****************************************************************************
*
*   Function name : Delay
*
*   Returns :       None
*
*   Parameters :    unsigned int millisec
*
*   Purpose :       Delay-loop
*
*****************************************************************************/
void Delay(unsigned int millisec)
{
	// mt, int i did not work in the simulator:  int i; 
	uint8_t i;

	while (millisec--) {
		for (i=0; i<125; i++) {
			asm volatile ("nop"::);
		}
	}
}
