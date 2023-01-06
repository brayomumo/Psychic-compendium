package main

import (
	"bufio"
	"fmt"
	"log"
	"os"
	"strconv"
	"time"
)

type Task struct {
	name         string
	description  string
	done         bool
	dateCreated  time.Time
	dateFinished time.Time
}

func newTask(name string, description string) *Task {

	task := Task{
		name:         name,
		description:  description,
		done:         false,
		dateCreated:  time.Now(),
		dateFinished: time.Time{},
	}
	return &task
}

var tasks []Task

func addTask(scanner bufio.Scanner) int {
	fmt.Println("Add a new task below: ")
	fmt.Println("Task Name: ")

	scanner.Scan()
	// Scan for multiple lines, Scanln for single line
	err := scanner.Err()

	if err != nil {
		log.Fatal(err)
	}
	task := scanner.Text()

	fmt.Println("Task Description: ")
	scanner.Scan()

	err = scanner.Err()
	if err != nil {
		log.Fatal(err)
	}
	desc := scanner.Text()

	tasks = append(tasks, *newTask(task, desc))
	log.Println("Task added successfully")

	return len(tasks)
}

func (t *Task) markAsDone() {
	t.done = true
	t.dateFinished = time.Now()
	tasks = append(tasks, *t)

}

func printTasks() {
	dateFormat := "2006-02-01 12:59pm"

	for index, task := range tasks {
		if task.done {
			fmt.Printf("%d: name: %s  -> Description: %s --> Done: %t  --> Date Finished: %s\n", index+1, task.name, task.description, task.done, task.dateFinished.Format(dateFormat))
		} else {
			fmt.Printf("%d: name: %s  -> Description: %s --> Done: %t\n", index+1, task.name, task.description, task.done)

		}
	}
}
func printPendingTasks() {
	for index, task := range tasks {
		if !task.done {
			fmt.Printf("%d: name: %s  -> Description: %s --> Done: %t\n", index+1, task.name, task.description, task.done)
		}
	}
}
func printPendingTasks() {
	for index, task := range tasks {
		if !task.done {
			fmt.Printf("%d: name: %s  -> Description: %s --> Done: %t\n", index+1, task.name, task.description, task.done)
		}
	}
}

func main() {
	scanner := bufio.NewScanner(os.Stdin)

	fmt.Println("Hello World\n Welcome to todo-cli")

	// label for the loop
context:
	for {
		fmt.Println(`
		To add new task enter E, 
		To finish a task enter F,
		To list unfinished tasks enter L, 
		To list All tasks enter A,
		To Delete tasks Enter D,
		To quit enter Q`)
		scanner.Scan()
		reply := scanner.Text()
		switch reply {
		case "E":
			l := addTask(*scanner)
			fmt.Printf("\nTasks added in array: %d\n ", l)
		case "F":
			fmt.Println("Enter task number to mark as complete:")
			scanner.Scan()
			num := scanner.Text()

			// convert index to int
			n, err := strconv.Atoi(num)
			if err != nil {
				log.Fatal(err)
			}
			task := tasks[n-1]
			tasks = append(tasks[:n-1], tasks[n:]...)
			task.markAsDone()
			// tasks  = append(tasks, task)

		case "L":
			printPendingTasks()
		case "A":
			printTasks()
		case "D":
			fmt.Println("Enter task number to mark as complete:")
			scanner.Scan()
			num := scanner.Text()
			n, err := strconv.Atoi(num)
			if err != nil {
				log.Fatal(err)
			}
			task := tasks[n-1]
			tasks = append(tasks[:n-1], tasks[n:]...)
			fmt.Printf("Task %s deleted successfully", task.name)

		case "Q":
			// break from the for loop using the label
			break context

		}
	}
	// l := addTask(*scanner)
	// fmt.Printf("\nTasks added in array: %d\n ", l)
	// // printTasks()
	// markAsDone(1)
	// printTasks()

}
