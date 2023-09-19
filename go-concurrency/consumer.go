package main

import (
	"log"
	"math/rand"
	"sync"
	"time"
)

type DataObject struct {
	Index int
	Name  string
}

type Consumer struct {
	Name      string
	Max_jobs  int
	Waiter    *sync.WaitGroup
	quit      chan int
}

func Newconsumer(name string, max_records int, waiter *sync.WaitGroup) Consumer {
	return Consumer{
		Name:      name,
		Max_jobs:  max_records,
		Waiter:    waiter,
	}
}

// This takes a channel from which to listen for data
// and constaly waits for data from it, skipping when channel is blocked
// it exists when it's own quit channel recieves data
func (c *Consumer) Run(channel chan DataObject) {
	defer c.Waiter.Done()
	// TODO: make `jobs_handled` unique to consumer to allow reuse of underutilized consumers :)
	// e.g by reducing wait time so remaining workers pick up jobs faster
	jobs_handled := 0
	for {
		select {
		case prod_data := <-channel:
			// exit when consumer hits max records
			if jobs_handled > c.Max_jobs {
				c.exit()
			}
			log.Printf(
				"Goroutine %s: Job Id: %d -> Name: %s\n", c.Name, prod_data.Index, prod_data.Name)
			jobs_handled++
		case <-c.quit:
			return
		default:
			// jitter to avoid thundering heard when channel is unblocked
			wait_time := rand.Intn(5)
			time.Sleep(time.Duration(wait_time) * time.Second)
			log.Printf("goroutine %s: Channel locked waiting for data...\n", c.Name)

		}
	}

}

func (c *Consumer) exit() {
	go func() {
		c.quit <- 1
	}()
}
