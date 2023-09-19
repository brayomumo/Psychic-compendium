# Go concurrency
use of goroutines and channels to build concurrent applications.  
This PoC implements a publisher-consumer wannabe architecture where there is a single publisher pushing jobs to  a buffered channel and a bunch of consumers reading from the channel concurrently.

## Usage
- Make sure you have go installed locally
- Clone the repo
- cd into `Psychic-compendium/go-concurrency`
- run `make run` (uses the default 2 consumers)
- To run with multiple consumers run:  
    - run `make run consumers=<no of consumers>`

## Architecuture
### Consumer
This is the `worker` that handles jobs from the the producer.
**Attributes**:  
- Name - random string to identify the process consuming data
- Max_jobs - No of jobs the consumer can handle
- Waiter - Waitgroup to control when the master processes exists
- quit - 'private' channel to trigger the quit operation on the channel

**Functionality**:
The Run method keeps track of number of jobs run by the consumer. The consumer then starts an inifinate loop, reading from either the shared channel(passed during initalization of Run method) or the quit channel for any termination 'signals'. Jobs from the shared channel are 'executed' and number of handled jobs incremented. If number of jobs_handled is greater than max_jobs for the consumer, the consumer produces the quit signal on its `quit` unbuffered channel which intern triggeres the exit process(a simple return)

### Producer
This create jobs(`DataObject`) and adds them to a shared buffered channel from which a bunch of consumers can pick them up for processing.
It adds Jobs to the shared channel untill the buffer is full after which it waits for consumers to pick data and create more space in the buffer before writing. This allows it to give consumers time to process data. However, this functiionality can be controlled by bumping up number of workers consuming from the channel using the `--consumer` flag during start up.

## Technical Notes
### Channels
- Buffered and unbuffered channels
- Unbuffered channels:
    - This holds only one value at a time
    - Recieve operation blocks until a value is sent to the channel
    - Send operation also blocks until there is a recieve operation on the channel 
- Buffered Channel:
    - Channel defines size of buffer(Amount of data it can hold before deadlocks occur)
    - Data Can be added to channel without waiting for recieve operation

### Deadlocks on Buffered channels
This occurs when Goroutine tries to send data to a channel thats already full or when a goroutine is trying to recieve data from an empty channel.
If a goroutine tries to send data to a a full channel it blocks until there is space in the channel buffer. If a goroutine tries to recieve data from the same channel and its also blocked, a deadlock occurs

When a goroutine tries to recieve data from an empty channel, it blocks until data is available, if a another goroutine is trying to send data to the same channel and is also blocked, a deadlock occurs.

This is sorted by using `SELECT` on the multiple channels to only read/write to channel that's 'free'. This even allows for use of both `buffered` and `unbuffered` channels.
