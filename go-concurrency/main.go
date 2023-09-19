// This is a PoC on Go concurrency and data sharing between goroutines
package main

import (
	"flag"
	"log"
	"math/rand"
	"strconv"
	"sync"
	"time"
)


func producer(wg *sync.WaitGroup, ch chan DataObject, max_record int) {
	defer wg.Done()
	i := 1
	for {
		object := DataObject{
			Index: i,
			Name:  "Producer Object-"+strconv.Itoa(i),
		}
		select{
		case ch <- object:
			log.Printf("Producer %d\n", i)
			i = i + 1
			if i > max_record {
				log.Println("Producer is Done!")
				return

			}
		default:
			log.Println("Buffer full, waiting for consumer to pick data....")
			time.Sleep(2 * time.Second)
		}
	}
	
}

func build_string(num int) string {
	return "goroutine-" + strconv.Itoa(num)
}

func main() {
	workers := flag.Int("consumers", 2, "Number for workers")
	flag.Parse()
	no_of_workers := *workers

	log.Printf("Starting app with %d Consumers\n",no_of_workers)

	// unbuffered channel holds one data object per time
	// send value channel_name <- data-to-send
	// variable_name := <-channel_name
	// var msq_bus chan DataObject = make(chan DataObject)
	MAX_RECORD := 100
	// buffered channel
	var shared_chan chan DataObject = make(chan DataObject, 10)

	
	// wait group to make sure main thread does not
	// terminate before threads finish
	var wait_group sync.WaitGroup

	// no of processes to wait for
	wait_group.Add(1 + no_of_workers)

	
	// Initialize Producer, we don't have to worry about channel blocking
	// since its a buffered channel and producer has wait time when channel
	// is blocked(no deadlocks)
	// TODO: Make multiple producers to make it interesting :)
	go producer(&wait_group, shared_chan, MAX_RECORD)

	// initialize consumers
	for i := 0; i < no_of_workers; i++ {
		go func(i int){
			name := build_string(i)
			//create consumer
			max_jobs := rand.Intn(MAX_RECORD/no_of_workers)
			cons := Newconsumer(name, max_jobs, &wait_group)
			cons.Run(shared_chan)
		}(i)
	}

	// wait for goroutines to finish
	wait_group.Wait()
	// close shared channel
	close(shared_chan)
}